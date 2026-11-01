# ETF 估值观察站

个人 ETF 估值监控网站，采用 AKShare 公开数据采集，不依赖理杏仁 API。

## 监控范围
- 标普500（.INX）
- 中证红利低波（H30269）
- 纳斯达克100（.NDX）
- 中证主要消费（000932）
- 科创50（000688，组合参考）

## 部署
1. 在仓库 Settings → Pages → Build and deployment 中选择 **GitHub Actions**。
2. Actions 工作流每周五 08:30 UTC（北京时间 16:30、日本时间 17:30）自动运行，也支持手动触发。
3. 首次运行完成后，网站地址为 https://fangbiaomail-code.github.io/ETF/

## 数据口径与限制
- AKShare 的中证指数历史接口可提供部分中国指数的滚动市盈率；H30269 的中证估值文件可提供 PE 与股息率历史。
- AKShare 的标普500和纳斯达克100行情接口提供指数行情，不等于 PE 历史估值。未找到可核验的 PE 历史数据时，网站会显示“暂无可靠数据”，不会伪造。
- 百分位按可获取的有效历史估值样本计算，并显示数据起始日期；不应把不同指数的百分位直接比较。
- 本网站仅供研究，不构成投资建议。

## 本地运行
```bash
pip install -r requirements.txt
python scripts/update_data.py
```
