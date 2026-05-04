# EPDZ 文件结构与 bbox 抽取 Pipeline 说明

这份文档解释两件事：

1. `*.epdz` 文件内部大致如何组织数据。
2. 当前脚本如何从 EPDZ 中抽取页面、元件、连接和 bbox，并把它们叠加到 PDF 页面上。

这里说的 bbox 指 bounding box，也就是一个图形对象在页面坐标系中的外接矩形，通常写成：

```json
[x0, y0, x1, y1]
```

## 1. EPDZ 是什么

从当前样本看，`.epdz` 本质上是一个压缩包。脚本用 `py7zr` 解压它：

```python
with py7zr.SevenZipFile(epdz_path, mode="r") as zf:
    zf.extractall(path=workdir)
```

解压后，最重要的入口是：

```text
manifest.db
```

这是一个 SQLite 数据库。除此之外，还会有若干资源文件，例如页面 SVG：

```text
packages/pages/items/pagesvg/*.svg
```

所以可以把 EPDZ 理解成两层：

- `manifest.db`：结构化索引，告诉我们有哪些页面、元件、连接、属性、资源引用。
- `packages/.../*.svg`：页面实际图形资源，里面有线条、文字、符号、颜色等矢量图形。

## 2. manifest.db 中的主要表

当前脚本主要使用这些表。

### 2.1 page_package

页面表。

每一行代表一个 EPLAN 页面。常用字段：

```text
packageid
name
```

例如第 11 页可能是：

```text
packageid = 49
name = ESS_Sample_Macros-4_3001
```

页面的页号、页类型、图号、位置代号等不直接在 `page_package` 里，而是在 `property` 表里。

### 2.2 function_package

功能/元件表。

每一行代表一个 EPLAN function。这里的 function 不一定等于“最终唯一设备”，它更像“页面上的一个功能实例”。

常用字段：

```text
packageid
name
ep1140
ep1240
```

`name` 很关键，因为它经常带有 SVG group id 的线索，例如：

```text
=+-GP1_17_54615
```

这里末尾 `_17_54615` 可以映射到页面 SVG 里的：

```text
Id17_54615
```

### 2.3 page_functions

页面与 function 的关联表。

常用字段：

```text
pageid
functionid
```

它告诉我们某个 `function_package.packageid` 出现在某个 `page_package.packageid` 上。

注意：这个表不是完整答案。当前样本里，第 11 页 `property` 里有 63 个 `functions` 引用，但 `page_functions` 只列出了 44 个 function。剩下的部分需要从页面属性里补。

### 2.4 mergedconnection_package

连接表。

每一行代表一条 EPLAN 连接。脚本读取它来生成 `wires`。

连接端点主要存在 `property` 表中，例如：

```text
31019 = endpoint A
31020 = endpoint B
```

端点值通常长这样：

```text
=+-QA1:2/T1
+-XD4:6
```

脚本会把它们拆成：

```json
{
  "device": "QA1",
  "pin": "2/T1"
}
```

### 2.5 item

资源引用表。

页面 SVG 文件不是直接写在 `page_package` 中，而是由 `item` 表指向。脚本查找：

```sql
SELECT packageid, type, locator, referenced
FROM item
WHERE type='pagesvg'
```

如果 `locator` 是 SVG 文件名，就拼成：

```text
packages/pages/items/pagesvg/<locator>
```

### 2.6 property

属性表，是 EPDZ 里信息最多、也最杂的一张表。

常用字段：

```text
packageid
propname
propid
propindex
value
```

脚本读取不同 package 的属性，例如：

页面属性：

```text
11000 = EPLAN 页号
11009 = 页面代号，比如 #001/1
11017 = 页面类型
1640  = location
functions / functions[n] = 页面引用的 function source ref
interruptionpoints / interruptionpoints[n] = 页面引用的中断点 source ref
```

function 属性：

```text
1140  = device tag
1240  = location tag
20001 = full tag
20026 = type text
20031 = function text
20038 = pins
20215 = component tag
```

connection 属性：

```text
31019 = endpoint A
31020 = endpoint B
31004 = color
31007 = wire size
31003 / 31000 = length
```

## 3. 页面 SVG 如何组织图形

每个页面一般有一个 `pagesvg`。SVG 里有很多元素：

```xml
<g id="Id17_54615">
  ...
</g>
```

`<g>` 是 SVG group。EPLAN 会把一些 function、连接点、中断点或图形对象组织成 group，并给它们类似这样的 id：

```text
Id17_54615
Id59_54655
Id70_2765
```

脚本利用这个规律做映射：

```text
function_package.name: =+-GP1_17_54615
SVG group id:        Id17_54615
```

或者：

```text
page property source ref: 25/59/54655/0
SVG group id:             Id59_54655
```

这就是为什么现在能把 EPDZ 的结构化数据和 SVG 里的图形位置连起来。

## 4. 为什么 bbox 不是 EPDZ 直接给出的

至少在当前样本中，`manifest.db` 没有一个简单字段直接写：

```text
这个元件的 bbox 是 x0,y0,x1,y1
```

它提供的是：

- 这个页面有哪些 function。
- 这个 function 的工程代号、引脚、类型、连接关系。
- 页面有哪些 SVG 资源。
- function 名字或页面引用里有可映射到 SVG group 的 id。

真正的几何位置在 SVG 里面，但 SVG 也不是直接给每个 group 写 bbox。SVG 只写了图元：

```xml
<path d="..." />
<line x1="..." y1="..." x2="..." y2="..." />
<rect x="..." y="..." width="..." height="..." />
<text x="..." y="...">...</text>
```

浏览器里有 `getBBox()` 这种运行时 API 可以算 bbox，但静态 Python 脚本读取 XML 时没有现成的浏览器渲染引擎。于是脚本只能自己算：

1. 遍历 group 里的子元素。
2. 对每种元素计算局部 bbox。
3. 应用 SVG `transform` 矩阵。
4. 把所有子元素的 bbox 合并成完整 group bbox。
5. 再排除 `text` 元素，只用非文本绘制元素合并成元件主体 bbox。

这就是“估算”的来源。

更准确地说：

- 对 `line`、`rect`、`circle`、`ellipse` 这类基础图形，bbox 可以算得比较准。
- 对 `path`，脚本解析路径命令并取关键点外接框，但曲线的真正极值点可能不在控制点上，所以它是近似。
- 对 `text`，SVG 不一定给出文字真实宽高，脚本只能按字符数和假设字体大小估算；所以文本只进入 `full_bbox`，不会进入默认画框用的 `bbox`。
- 对整个 group，`full_bbox` 包含 group 内所有子元素；`symbol_bbox` 排除文本，只包含实际绘制图形；compact JSON 里的默认 `bbox` 优先使用 `symbol_bbox`。

所以 bbox 不是因为我们不想精确，而是因为 EPDZ 当前暴露给脚本的数据不是“现成元件框”，而是“结构化工程数据 + SVG 矢量图”。我们要从 SVG 图形反推 bbox。

## 5. 当前处理 Pipeline

当前和 EPDZ bbox 相关的脚本有三个：

```text
scripts/epdz_to_connection_json.py
scripts/inspect_eplan_pdfs.py
scripts/render_epdz_page_bboxes.py
```

### Step 1: 解压 EPDZ

入口：

```python
extract_epdz(epdz_path, workdir)
```

输入：

```text
data/epdz_files/ESS_Sample_Macros.epdz
```

输出：

```text
<temp>/manifest.db
<temp>/packages/pages/items/pagesvg/*.svg
```

目标是拿到 SQLite 数据库和页面 SVG。

### Step 2: 读取数据库主表

入口：

```python
build_output(db_path, extracted_root)
```

读取：

```sql
SELECT packageid, name FROM function_package
SELECT packageid, name FROM mergedconnection_package
SELECT packageid, name FROM page_package
SELECT pageid, functionid FROM page_functions
```

同时用 `load_properties()` 批量读取这些 package 的属性。

这一步产出原始结构：

```json
{
  "pages": [],
  "devices": [],
  "function_occurrences": [],
  "wires": []
}
```

### Step 3: 找到每页 SVG

脚本从 `item` 表里找 `type='pagesvg'` 的资源：

```sql
SELECT packageid, type, locator, referenced
FROM item
WHERE type='pagesvg'
```

然后拼出 SVG 路径：

```text
packages/pages/items/pagesvg/<locator>
```

得到映射：

```text
page package id -> page svg file
```

### Step 4: 从 SVG 计算 bbox

入口：

```python
collect_group_bboxes(svg_path)
```

它会递归遍历 SVG：

```text
svg
  g id="Id17_54615"
    path
    line
    text
```

递归过程中会把父级和当前元素上的 `transform` 逐层相乘，得到每个图元在页面 SVG 坐标系里的绝对坐标，再参与 bbox 合并。

对每个 `g id="Id..."`，脚本会同时生成两个 bbox：

- `bbox`：完整 group bbox，包含文字、标签等所有子元素。
- `symbol_bbox`：元件主体 bbox，只包含非文本绘制元素，例如 `path`、`line`、`rect`、`circle`、`ellipse`、`polyline`、`polygon`、`image`。

生成结构类似：

```json
{
  "Id17_54615": {
    "bbox": [82.5, 107.0, 107.2, 126.111],
    "symbol_bbox": [82.5, 107.0, 102.5, 122.0],
    "title": ""
  }
}
```

这一步得到的是 SVG 坐标系下的 bbox，不是 PDF 像素坐标。

### Step 5: 建立 function occurrence

以前脚本主要按 `device_id` 聚合，这会导致同名设备/同短名设备覆盖 bbox。

现在新增了：

```json
"function_occurrences": []
```

每个 occurrence 代表页面上的一个 function 实例，结构类似：

```json
{
  "package_id": 484,
  "source_ref": null,
  "name": "=+-GP1_17_54615",
  "device_id": "GP1",
  "raw_id": "=+-GP1",
  "type": "泵,2 个连接点",
  "pages": ["ESS_Sample_Macros-4_3001"],
  "svg_id": "Id17_54615",
  "bbox_by_page": {
    "ESS_Sample_Macros-4_3001": {
      "svg_id": "Id17_54615",
      "bbox": [82.5, 107.0, 107.2, 126.111],
      "symbol_bbox": [82.5, 107.0, 102.5, 122.0]
    }
  }
}
```

这里的核心是：

```text
function package -> svg id -> svg group bbox / symbol bbox
```

### Step 6: 从页面属性补充遗漏 occurrence

第 11 页暴露了一个问题：

- 页面属性 `functions` 有 63 个引用。
- `page_functions` 只给了 44 个。

所以脚本现在还会读取页面属性：

```text
functions
functions[0]
functions[2]
...
interruptionpoints
interruptionpoints[0]
...
```

然后把 source ref 转成 SVG id：

```text
25/59/54655/0 -> Id59_54655
25/70/2765/0 -> Id70_2765
```

这样可以补上 `page_functions` 没列出来、但 SVG 里确实存在的对象。

第 11 页现在的结果是：

```text
function_occurrences = 68
带 bbox 的 occurrence = 58
```

其中 68 = 63 个 functions + 5 个 interruptionpoints。

剩下 10 个没有 bbox，是因为它们虽然在 `function_package` 里，但页面 SVG 中找不到对应的 `Id17_xxx` group。当前脚本不能凭空知道它们的位置，只能等后续从父设备或连接点关系推断。

### Step 7: 聚合 devices

脚本仍然保留 `devices`。

它把多个 function occurrence 合并成一个设备，例如：

```text
=VN01.HW01+B4-BL1
=VN01.HW02+B4-BL1
=VN01.HW03+B4-BL1
=VN01.HW04+B4-BL1
```

可能都会归到：

```text
BL1
```

这个聚合适合做连接、设备列表、pin 汇总，但不适合直接画“每个页面实例”的 bbox，因为会丢失同设备的多个 occurrence。

所以现在的规则是：

- 画框优先使用 `function_occurrences`。
- 做设备汇总可以使用 `devices`。

### Step 8: 生成 wires

脚本读取 `mergedconnection_package`，再从属性里解析端点：

```text
31019 endpoint A
31020 endpoint B
```

输出结构类似：

```json
{
  "id": "W342",
  "raw_id": "25/18/57607/0",
  "connections": ["QA1:2/T1", "QA2:2/T1"],
  "endpoints": [
    {"device": "QA1", "pin": "2/T1"},
    {"device": "QA2", "pin": "2/T1"}
  ]
}
```

目前 wire bbox 是端点设备 bbox 的 union。这不是导线真实路径，只是一个粗略范围。因此 wire bbox 现在不适合当“真实线段几何”使用。

### Step 9: 生成 compact inspection JSON

入口：

```text
python scripts/inspect_eplan_pdfs.py
```

输出：

```text
output/epdz_inspection/ESS_Sample_Macros.compact.json
```

compact JSON 按页组织：

```json
[
  {
    "page": 11,
    "info": {},
    "function_occurrences": [
      {
        "id": "F484",
        "device_id": "GP1",
        "bbox": [82.5, 107.0, 102.5, 122.0],
        "symbol_bbox": [82.5, 107.0, 102.5, 122.0],
        "full_bbox": [82.5, 107.0, 107.2, 126.111]
      }
    ],
    "devices": [],
    "wires": []
  }
]
```

这里的 bbox 字段含义是：

- `bbox`：默认画框使用的 bbox，当前优先等于 `symbol_bbox`。
- `symbol_bbox`：只由非文本绘制元素计算出的主体 bbox。
- `full_bbox`：完整 SVG group bbox，包含文字和标签。

这是后续可视化最方便读的格式。

### Step 10: 把 bbox 叠加到 PDF

入口：

```text
python scripts/render_epdz_page_bboxes.py --page 11
```

它读取：

```text
output/epdz_inspection/ESS_Sample_Macros.compact.json
data/eplans/#000_1.pdf
```

输出：

```text
output/epdz_bbox_overlay/page_0011_overlay_bbox_fields.png
```

这里要做坐标转换：

```text
EPDZ/SVG 页面坐标: 420 x 297
PDF 页面坐标:      1191 x 842
```

转换方式是线性缩放：

```text
pdf_x = svg_x * (pdf_width / 420)
pdf_y = svg_y * (pdf_height / 297)
```

当前第 11 页的 SVG 坐标方向和 PDF 渲染方向一致，所以不需要 `flip_y`。

## 6. 为什么有的框看起来偏大或偏移

常见原因有四类。

### 6.1 group 包含的不只是元件本体

EPLAN 的 SVG group 可能包含：

- 符号本体
- 设备标签
- 功能文本
- 引线
- 小连接点
- 说明文字

脚本合并整个 group 的 bbox 时，会把这些都算进去。所以框可能比肉眼认为的元件更大。

### 6.2 text bbox 是估算

SVG text 的真实宽高依赖字体、字号、字形、浏览器排版。静态 XML 里不一定能直接拿到真实文字宽度。

当前脚本用简单规则估算：

```text
文字宽度 ~= 字符数 * 字号 * 0.6
```

所以文字参与 bbox 时，bbox 可能偏大或偏小。

### 6.3 path 曲线 bbox 是近似

对曲线来说，真正的 x/y 极值可能出现在曲线中间，不一定出现在端点或控制点。当前脚本没有完整求解 Bezier 极值，所以 path bbox 是近似。

### 6.4 EPDZ 页面对象不一定都有 SVG group

有些 function 出现在数据库里，但 SVG 里没有对应 group。这类对象可能是逻辑连接点、内部功能、或者被父对象图形包含了。

它们无法直接画 bbox，需要后续从父设备、连接端点或页面几何关系推断。

## 7. 当前输出该怎么理解

推荐这样看：

### function_occurrences

最适合用来画页面上的候选框。

优点：

- 保留每个页面实例。
- 不会因为同名设备聚合而丢框。
- 能覆盖 `functions` 和 `interruptionpoints`。

局限：

- bbox 是从 SVG group 反推的。默认 `bbox` 使用非文本主体框，完整 group 框保存在 `full_bbox`。
- 有些 occurrence 没有对应 SVG group。

### devices

适合看设备汇总。

优点：

- 能把多个 raw id 合成较短设备名。
- 适合连接关系和 pin 汇总。

局限：

- 不适合直接画页面所有实例，因为同设备多个 function 会被合并。

### wires

适合看端点连接关系。

优点：

- 能知道哪些 device:pin 相连。

局限：

- 当前 bbox 不是实际导线路径，只是端点 bbox union。

## 8. 后续如果要继续提高精度

可以按这个顺序继续做。

### 8.1 继续细化 symbol bbox

当前已经区分了完整框和主体框：

```json
{
  "bbox": [x0, y0, x1, y1],
  "full_bbox": [x0, y0, x1, y1],
  "symbol_bbox": [x0, y0, x1, y1]
}
```

其中 `bbox` 默认使用 `symbol_bbox`，`full_bbox` 保留完整 group 范围。后续还可以继续细化 `symbol_bbox`：

- 过滤非常细长的引线。
- 对传感器、连接点这类小符号设置最小尺寸兜底。
- 对父子 group 做合并，避免某些主体图形被拆到父级或相邻 group。

### 8.2 为无 bbox 的 occurrence 推断位置

对于第 11 页剩下的 10 个 `CM1.1` function，可以尝试：

- 找同一设备 `CM1.1` 已有 bbox。
- 根据 pin、连接点、source ref 顺序推断局部位置。
- 或者从 SVG 里寻找被父 group 包含但没有独立 id 的小图形。

### 8.3 用浏览器或 SVG 渲染引擎计算 bbox

如果要更接近真实显示，可以考虑调用浏览器环境里的 `getBBox()`，或者使用更完整的 SVG 渲染库。

代价是 pipeline 会更重，依赖也更多。

## 9. 一句话总结

EPDZ 没有直接给出“每个元件的最终 bbox”。它给的是数据库里的工程对象和页面 SVG 里的矢量图形。当前 pipeline 做的事情是：

```text
解压 EPDZ
-> 读取 manifest.db
-> 找到页面、function、connection、property
-> 找到页面 SVG
-> 用 function/source_ref 映射 SVG group id
-> 汇总父子 transform，计算图元绝对坐标
-> 从 SVG 图元反推 full_bbox 和 symbol_bbox
-> 输出按页组织的 function_occurrences/devices/wires
-> 把 SVG 坐标线性映射到 PDF 页面坐标
-> 生成 overlay 图
```

所以 bbox 之所以有“估算”成分，是因为它不是 EPLAN 明确写好的元件框，而是我们从 SVG 图形结构反推出来的外接矩形。当前默认画框用的是 `bbox`，也就是非文本主体框；如果要调试完整 group 范围，可以看 `full_bbox`。
