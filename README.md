# 发票下载与凑单

从 QQ 邮箱把 2026 年发票 PDF 拉到本地，文件名带金额前缀；需要报销时按目标金额凑单并移走。

## 安装

需要 Python 3.10+。在仓库根目录：

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -e .
```

## 配置 QQ IMAP

1. 打开 [QQ 邮箱](https://mail.qq.com) → 设置 → 账户
2. 开启 IMAP/SMTP 服务，生成**授权码**（不是登录密码）
3. 复制 `.env.example` 为 `.env`，填入邮箱和授权码：

```
QQ_EMAIL=you@qq.com
QQ_AUTH_CODE=你的授权码
```

不要把 `.env` 提交到 git，也不要在聊天里发送授权码。

发票邮件请留在**收件箱**。只下载 `.pdf` 附件，不处理 OFD。默认只拉取 **2026-06-17 及之后**（含当天）的邮件；更早的票不自动拉取，可用 `--since` 覆盖。

## 命令

```bash
# 下载 2026-06-17 起（含当天）的收件箱 PDF；更早的票不自动拉取
python -m invoice download

# 预览凑单（不移动）
python -m invoice pack --amount 200 --folder 出差报销 --dry-run

# 把选中的发票移动到仓库根目录下的「出差报销」
python -m invoice pack --amount 200 --folder 出差报销
```

金额识别顺序：PDF 正文「价税合计 / 小写」→ 邮件主题 → 附件名。失败的文件进 `2026年发票/未识别/`，不参与凑单。

## 凑单规则

- 合计不能低于目标；可以超过
- 在所有 ≥ 目标的组合里，选超出最少的一组；超出相同则张数更少
- 发票池总额不够时只报告差额，不移动文件
- 已移走的发票不再出现在 `2026年发票/`，避免重复报销

## 测试

```bash
python -m unittest discover -s tests -v
```
