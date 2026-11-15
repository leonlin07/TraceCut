# TraceCut 谣言判别样本报告（2026-07-21）

> 通过 `tracecut judge <rumor>` CLI 跑通的第一批样本。纯免费检索路径（中英双语 Wikipedia + LLM 查询改写，无任何收费搜索 API）。每条产出 `judgment.json` + `citations.json`，含逐 claim 判别（SUPPORT/REFUTE/UNVERIFIABLE）+ 证据归因。

## 样本与判别结果

| 谣言 | 真值 | 系统判别 | 正确? |
|---|---|---|---|
| 2024 Nobel 物理学奖颁给 Hopfield & Hinton | 真 | **SUPPORT** | ✅ |
| 爱因斯坦 1921 年获诺贝尔物理学奖（光电效应） | 真 | **SUPPORT** | ✅ |
| 珠穆朗玛峰高度 8848.86m（2020 中尼联合测量） | 真 | **SUPPORT** | ✅ |
| 秦始皇兵马俑 1974 年由陕西农民打井偶然发现 | 真 | **SUPPORT** | ✅ |
| 埃菲尔铁塔 2024 奥运期间被拆除搬到里昂 | 假 | **REFUTE** | ✅ |
| SpaceX 星舰首次轨道试飞成功环绕并回收着陆 | 假 | **REFUTE** | ✅ |
| 哈佛研究证明每天一粒维 C 治愈所有感冒 | 不可证/假 | **UNVERIFIABLE** | ✅ |
| 某研究显示喝咖啡能让人寿命延长 50 年 | 不可证 | **SUPPORT**（误判） | ❌ |

**8 条中 7 条判别正确**（真命题全 SUPPORT、假命题全 REFUTE、维 C 案正确 UNVERIFIABLE）。

## 误判分析（咖啡案）

咖啡案是真实的判别错误，值得记录：
- claim `c_01 "存在某项研究显示喝咖啡能延长人类寿命"` 被误判为 SUPPORT。
- 原因是 **over-attribution（过度归因）**：判别器把"喝咖啡降低帕金森风险"的证据牵强解读为"间接支持寿命延长"。这是 fact-checking 的典型失败模式——判别器在证据不完全匹配时做了推理跳跃。
- 同时证据召回有噪声（返回了"法国""喝酒长期影响"等无关页面），因为查询改写把长句泛化成了过宽的实体。

这个错误正是 RQ1 要量化的对象：claim-evidence 图 + 更严格的判别 prompt（禁止"间接支持"类推理）应能降低 over-attribution。已纳入下一轮改进。

## 结论

- 判别骨架在纯免费路径下对**可核查事实型 claim**（谁获奖、什么时间、什么数据）判别准确率高。
- 失败集中在**模糊因果 claim**（"延长寿命""间接支持"），这是下一步要攻的——通过更严格的判别 prompt + 召回去噪。
- 不依赖任何收费搜索 API，骨架可直接用于扩规模实验。

## 复现

```bash
tracecut judge "2024 年诺贝尔物理学奖颁发给了 John Hopfield 和 Geoffrey Hinton。"
tracecut judge "埃菲尔铁塔在 2024 年巴黎奥运会期间被完全拆除并搬到里昂重建。"
```

输出：`tracecut/runs/judge_<slug>_<id>/judgment.json` + `citations.json`。
