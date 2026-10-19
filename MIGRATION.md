# BOLL 项目迁移说明

## 包含内容

- `project/`：策略源代码、股票池、测试、运行脚本及当前输出结果。
- `skills/a-stock-data/`：本项目使用的 A 股数据 skill。
- 当前 Codex 会话的只读共享副本：
  https://chatgpt.com/s/cx_6ab0d426f04481918be34c35b7c4764f

迁移包不包含 Codex/ChatGPT 登录凭据、API Key、浏览器状态或整个 `.codex` 用户目录。

## 在新电脑恢复

1. 安装 Python 3.10 或更高版本，并安装 Codex/ChatGPT 桌面应用。
2. 将 `project` 目录复制到希望使用的位置。
3. 将 `skills/a-stock-data` 复制到：

   ```text
   %USERPROFILE%\.codex\skills\a-stock-data
   ```

4. 重启 Codex，然后在 PowerShell 中执行：

   ```powershell
   cd <迁移后的 project 目录>
   python -m pip install -e ".[test]"
   python -m unittest discover -s tests -v
   python -m boll_strategy quote
   ```

5. 打开上面的会话共享链接查看原始需求与实现记录。在新电脑的项目目录中新建任务，即可继续开发。

## 迁移同一个可继续编辑的 Codex 任务

共享链接是只读副本，不能代替原任务。若需要让同一个任务在两台电脑之间继续运行，应先把项目纳入 Git，
在目标电脑保存同一个 Git 仓库项目并配置为 Codex 远程主机，然后在桌面应用任务底部选择运行位置并执行
`Hand off`。不要直接复制整个 `%USERPROFILE%\.codex`，其中可能包含设备相关状态和凭据。
