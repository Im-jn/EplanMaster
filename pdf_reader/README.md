# PDF Reader

这个目录包含一个面向 Eplan 智能 PDF 的前端阅读器，用来做两件事：

- 显示 `data/eplans` 下的原始 PDF 页面。
- 在页面上叠加“可点击对象层”，点击后查看对应的 PDF 源码片段。

当前支持的对象类型：

- 矢量路径：由 PDF 内容流里的 `m`、`l`、`c`、`re`、`S`、`f`、`B` 等绘图命令生成。
- 超链接注释：由页面 `/Annots` 中的 `/Subtype /Link` 对象生成。

## 先生成阅读器数据

在仓库根目录运行：

```powershell
python scripts/build_pdf_reader_data.py
```

默认会读取：

- 输入目录：`data/eplans`

并写入：

- 输出目录：`pdf_reader/public/reader-data`

如果你在 `data/eplans` 里新加了一个 PDF，需要重新执行这一步。

## 启动前端

进入 `pdf_reader` 后运行：

```powershell
npm install
npm run dev
```

如需生产构建：

```powershell
npm run build
```

## 最短运行流程

首次运行：

1. 把 PDF 放到 `data/eplans`
2. 在仓库根目录运行 `python scripts/build_pdf_reader_data.py`
3. 进入 `pdf_reader`
4. 运行 `npm install`
5. 运行 `npm run dev`

之后日常使用：

1. 如果只是打开已经处理过的 PDF，直接在 `pdf_reader` 下运行 `npm run dev`
2. 如果你新增了 PDF，或者替换了原来的 PDF，先重新运行 `python scripts/build_pdf_reader_data.py`
3. 再启动或刷新前端

## 新增 PDF 时该做什么

假设你新增一个文件到：

```text
data/eplans/你的新文件.pdf
```

那么只需要做两步：

```powershell
python scripts/build_pdf_reader_data.py
cd pdf_reader
npm run dev
```

脚本会自动：

- 扫描 `data/eplans` 下所有 `.pdf`
- 复制 PDF 到前端静态目录
- 重新生成 `manifest.json`
- 为每个 PDF 生成页面对象索引数据

也就是说，你**不需要手动改前端代码，也不需要手动登记新文件**。

## 当前实现说明

这个版本是一个可用的 MVP，重点是“点击页面区域，回看 PDF 源码”。

它已经适合：

- 浏览样本 PDF
- 点选矢量线段/路径区域
- 点选链接跳转区域
- 在右侧查看对象引用、命令类型和源码片段

它暂时还没有做到：

- 精确到文字 glyph 级别的逐字点击
- 所有图像/表单/XObject 的统一对象级映射
- 从源码片段反向高亮整条复杂绘制链路

如果后续要继续增强，最自然的下一步是：

- 增加文字对象索引层
- 增加 Form XObject / Image XObject 的独立点击层
- 增加源码面板中的对象跳转与跨页跳转追踪
