# py_tool_reimbursement
发票 + 付款截图 自动拼版工具：把目录里的电子发票 PDF 和手机支付截图按文件名自动分组配对，
调用视觉大模型裁剪截图关键区域（商户、金额、支付时间、支付方式），拼成便于打印报销的
A4 PDF——每页上下两联，发票矢量占一联、截图占一联。

```shell
 ( ➜  reimbursement git:(master) python3 ~/Projects/reimbursement/merge_fp.py --help
usage: merge_fp.py [-h] [--no-cache] [src]

发票 + 付款截图 自动拼版为 A4 PDF

positional arguments:
  src         材料目录（默认 ~/Downloads/fp）

optional arguments:
  -h, --help  show this help message and exit
  --no-cache  忽略缓存，所有截图重新识别（新结果写回缓存）
```

## 功能特性

- **自动分组**：按文件名前缀（去尾部数字）分组，如 `yyl.pdf` / `yyl1.jpg` / `yyl2.jpg` → `yyl` 组；
  组内发票在前、截图按编号排序，两两配对占满一页
- **AI 智能裁剪**：截图发给视觉模型，返回关键信息区域的百分比坐标；
  兼容任意 OpenAI 兼容接口，默认智谱 `glm-5.3-flash`
- **本地缓存**：裁剪坐标按「文件名 + 修改时间 + 大小」缓存，文件未变不重复调接口，
  重跑秒出；`--no-cache` 强制全量重新识别
- **并发识别**：线程池并发调用（默认 3，可配），遇 429 限流自动退避重试
- **降级兜底**：AI 失败不中断——手机竖屏比例截图取上半部，其余取默认区域；
  失败结果不写缓存，下次自动重试
- **打印友好**：截图页 300 DPI 合成，发票页保持矢量；8pt 边距避开打印机不可打印区

## 安装

Python 3.8+

```bash
pip install pillow pypdf
cp .env.example .env    # 填入你的 AI_API_KEY
```

## 使用

```bash
python3 merge_fp.py [材料目录] [--no-cache]
```

- 材料目录省略时默认 `~/Downloads/fp`
- 输出 `~/Downloads/<目录名>_合并.pdf`，完成后自动用 Chrome 打开
- `--no-cache`：忽略缓存，所有截图重新识别（新结果写回缓存）

示例目录结构：

```
~/Downloads/fp/
├── yyl.pdf      # 发票
├── yyl1.jpg     # 该发票对应的付款截图
├── yyl2.jpg
├── yw.pdf
└── yw.png
```

## 配置（.env）

| 变量 | 说明 | 默认值 |
|---|---|---|
| `AI_API_KEY` | API Key（[智谱申请](https://open.bigmodel.cn)） | 空 = 不用 AI，走兜底裁剪 |
| `AI_BASE_URL` | OpenAI 兼容接口地址 | `https://open.bigmodel.cn/api/paas/v4` |
| `AI_MODEL` | 视觉模型名 | `glm-5.3-flash` |
| `AI_CONCURRENCY` | 识别并发数，限流时调小 | `3` |

## 依赖

- [Pillow](https://python-pillow.org/)：截图裁剪与页面合成
- [pypdf](https://pypdf.readthedocs.io/)：发票矢量页合并
