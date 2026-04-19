# EplanMaster

这个仓库现在带了一个直接查看 PDF 原始结构的检查脚本：

- 脚本: `scripts/inspect_eplan_pdfs.py`
- 默认输入目录: `data/eplans`
- 默认输出目录: `output/pdf_inspection`
- conda 环境名: `Eplan`

运行方式:

```powershell
conda run -n Eplan python scripts/inspect_eplan_pdfs.py
```

输出重点:

- `output/pdf_inspection/README.md`: 总入口
- `output/pdf_inspection/<PDF名>/overview.md`: 每个 PDF 的结构说明
- `output/pdf_inspection/<PDF名>/summary.json`: 完整摘要
- `output/pdf_inspection/<PDF名>/pages_readable.json`: 按页整理的可读 JSON
- `output/pdf_inspection/<PDF名>/xref_raw_object.txt`: xref 流对象原始文本
- `output/pdf_inspection/<PDF名>/pages/page_xxxx/bundle.txt`: 页面原始对象、内容流、超链接、图片对象集中查看
- `output/pdf_inspection/<PDF名>/pages/page_xxxx/readable.json`: 单页可读说明 JSON

脚本尽量只用 Python 标准库，这样环境里不需要额外安装第三方包。
