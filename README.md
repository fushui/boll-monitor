# A 股短线 BOLL 下轨策略

这是一个面向图片中股票池的日线筛选与回测工具。核心约束不会被评分稀释：**只有收盘价严格低于 BOLL 下轨的股票才可能成为买入候选，建议买入限价也严格低于下轨**。卖出目标可选 `mid` 或 `upper`。

## 策略规则

- 指标：前复权日 K，BOLL(20, 2)，总体标准差 `ddof=0`，RSI(14)。
- 买入候选硬条件：`close < lower`、RSI 不高于 45、量比在 0.3–2.5、单日跌幅不低于 -8%、收盘位于当日振幅上方至少 20%。
- 委托参考价：`lower × (1 - 0.1%)`，因此严格小于下轨。每日收盘后的信号用于下一交易日限价观察，不把收盘后才知道的数据当作盘中成交依据。
- 卖出：由 `exit_line` 选择动态中轨或上轨；回测另有 7% 止损与 10 个交易日时间止损。
- 评分（0–100）：下轨穿透 30、RSI 超卖 25、K 线回收 15、量比 10、当日企稳 10、回到中轨空间 10。评分只排序，不能绕过硬条件。

默认参数都能在 [config.example.json](config.example.json) 中修改。行情实现遵循
[a-stock-data skill](https://github.com/simonlin1212/a-stock-data) 的数据源优先级，默认直连腾讯财经日 K 接口，无需 API Key。

## 股票池

[data/stock_pool.csv](data/stock_pool.csv) 收录截图中可辨认的 61 个唯一代码。截图画面共出现 63 行，其中 `SH.600016 民生银行` 与 `SH.601818 光大银行` 因长截图拼接各重复一次，程序按代码去重。截图顶部虽然显示“共 65 只股票”，但画面中没有另外两只股票的名称或代码，因此没有猜测补入。

## 安装

```powershell
cd F:\code\boll
python -m pip install -e ".[test]"
```

默认数据源为腾讯前复权日线，代码会在三个入口之间故障切换，并校验响应结构、日期、重复记录、OHLC 关系与非正价格。腾讯成交量单位为“手”；这里只计算相对量比，因此无需换算成股。

## 当天实时行情

查询股票池最新价、涨跌幅、最高/最低价、成交额、换手率、量比、估值和涨跌停价：

```powershell
python -m boll_strategy quote --output output/realtime_quotes.csv
```

每日 `scan` 默认也会批量查询实时行情，并把 `realtime_price`、`quote_time`、
`realtime_below_lower`、`realtime_at_or_below_buy_limit` 和实时目标空间等字段合并到筛选结果。
盘中运行时，当日未收盘 K 线不会参与 BOLL 指标计算；实时价格只用于判断当前是否仍在下轨以下。
如只需要盘后日线结果，可传入 `--no-realtime`。

## 美股 BOLL 版本

美股策略页面发布后访问：<https://fushui.github.io/boll-monitor/us/>。默认股票池在
`data/us_stock_pool.csv`，每个美股交易日收盘后由 GitHub Actions 扫描、更新页面并通过 PushPlus 推送。
手动扫描：

```powershell
python -m us_boll.scan --range 3y
python -m us_boll.scan --range 3y --push
python build_us_pages.py
```

计算规则复用本项目的 BOLL(20,2)、RSI(14)、严格收盘价低于下轨、`下轨 × 0.999` 限价、评分、T+1 限价成交回测、7%止损和10个交易日时间止损。卖出目标可在页面切换中轨或上轨。美股不设置A股式涨跌停约束。

行情来自腾讯财经 ifzq 公共接口（美股代码带 `.OQ`/`.N`/`.A` 交易所后缀，非官方、无可用性保证）；网页只展示日线扫描快照，不提供券商下单，也不是保证实时的报价服务。盘中实时刷新走腾讯 `qt.gtimg.cn` 美股行情，BOLL/RSI 日线信号仍来自最近一次完整扫描。GitHub Actions 按美国常规交易时段收盘后运行，存在调度延迟；行情源不可用时，本次工作流会报错，不会用空数据覆盖页面。

## 每日筛选

卖到中轨：

```powershell
python -m boll_strategy scan --provider astock --exit-line mid
```

卖到上轨：

```powershell
python -m boll_strategy scan --provider astock --exit-line upper
```

结果：

- `output/daily_scan.csv`：全股票池的条件明细。
- `output/daily_scan_candidates.csv`：仅合格候选，按分数降序。
- `output/daily_scan_errors.json`：若个别股票失败，记录原因且不中断整池。

Windows 任务计划程序可以每天收盘后调用：

```powershell
powershell -ExecutionPolicy Bypass -File F:\code\boll\run_daily.ps1 -ExitLine mid
```

## 回测

```powershell
python -m boll_strategy backtest --provider astock --start 2023-01-01 --end 2026-09-18 --exit-line mid
python -m boll_strategy backtest --provider astock --start 2023-01-01 --end 2026-09-18 --exit-line upper
```

回测采用无未来函数的近似成交规则：T 日收盘生成信号，T+1 日只有最低价触及低于 T 日下轨的限价才成交；若同一天同时触发止损和目标，按较保守的止损先发生处理。结果是研究工具，不代表收益承诺。

## 离线 CSV

需要复现实验或断网运行时，可给每只股票准备一个 `SH.600519.csv` 形式的文件，至少包含 `date,open,high,low,close,volume`：

```powershell
python -m boll_strategy scan --provider csv --data-dir .\my_kline_data
```

## 测试

```powershell
python -m unittest discover -s tests -v
```

测试覆盖数据源代码映射与解析校验、BOLL 口径、严格下轨条件、Mid/Upper 切换，以及回测买入价始终低于信号日下轨。
