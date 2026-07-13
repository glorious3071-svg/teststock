# News Vector Signal Framework Report

生成时间：2026-07-12

## 结论

向量索引能提升新闻信号的有效性，但更适合作为“语义特征层”，而不是直接替代现有规则框架。

本次回测使用 2019-2026 年 CSI 年度样本，验证窗口为 2021-2025 年。无约束调参下，纯向量 ML 排序的历史均值超额最高；但考虑样本年数只有 5 年，且投资框架需要保留规则约束，当前建议是：

- 研究最优：`vector_ml=100%`，均值超额 `6.17%`，Top/Bottom spread `6.39%`，Spearman `11.33%`。
- 规则优先最优：`rule=75% + base_ml=25% + vector_ml=0%`，均值超额 `5.52%`，spread `3.50%`，Spearman `1.92%`。
- 规则优先且强制纳入向量：`rule=50% + base_ml=25% + vector_ml=25%`，均值超额 `4.13%`，spread `3.28%`，Spearman `4.95%`。

投资框架建议采用“规则优先最优”作为当前主框架；向量层暂时作为观察和二级增强，不直接提高主仓位权重。原因是向量层在 rank correlation 和纯模型收益上有价值，但一旦要求规则权重不低于 50%，它尚未超过 `rule=75% + base_ml=25%`。

## 体系结构

推荐的新体系分五层：

1. 新闻原始层：`news_article` 保存标题、正文、来源、发布时间。
2. 模型加工层：`news_extraction` 保存摘要、主题、事件类型、情绪、强度、置信度。
3. 向量索引层：对 `标题 + 摘要 + 主题 + 事件类型 + reasoning` 建向量，索引键保持 `article_id/event_id/theme/date/model_version/content_hash`。
4. 语义特征层：按年度配置窗口聚合主题级特征，包括新颖度、重复密度、语义广度、相似历史收益、多源强度。
5. 投资评分层：把向量特征并入 CSI scorecard walk-forward 训练，和规则分、原 ML 分做组合调参。

本次没有调用外部 embedding 或 LLM API。实验脚本使用本地 hashed n-gram 向量模拟向量索引能力，后续可以替换为 FAISS、pgvector、Milvus 或本地 embedding 模型，保持特征接口不变。

## 向量特征

脚本输出主题级向量特征到 `data/ml/news_vector_theme_features.csv`：

- `vector_event_count`：窗口内主题事件数量。
- `vector_sentiment_score`：情绪方向、强度、置信度、传播次数合成分。
- `vector_novelty_score`：相对历史相似事件的新颖度。
- `vector_duplicate_density`：窗口内语义重复程度。
- `vector_semantic_breadth`：主题内部语义扩散程度。
- `vector_source_diversity`：来源多样性。
- `vector_similar_excess`：相似历史事件对应主题的后验超额收益。
- `vector_theme_strength`：新颖度、情绪和重复惩罚后的综合主题强度。

合并后的年度 CSI 样本写入 `data/ml/industry_scorecard_vector_features.csv`，共 `1379` 行，向量主题特征 `107` 行。

## 回测设置

脚本：`scripts/backtest_news_vector_framework.py`

命令：

```bash
python3 scripts/backtest_news_vector_framework.py --from 2019 --to 2026 --target-year 2026 --validate-to 2025
```

调参范围：

- Ridge alpha：`1, 3, 10, 30, 100`
- 组合权重网格：`0, 0.10, 0.25, 0.50, 0.75, 1.00`
- 验证方式：walk-forward，至少 2 个训练年。
- 验证年份：2021-2025。
- 评价指标：Top-K 相对沪深300超额、Top/Bottom spread、Spearman。

## 回测结果

| 框架 | 权重 | 均值超额 | Spread | Spearman |
| --- | --- | ---: | ---: | ---: |
| 规则基线 | rule=100% | 3.08% | 2.32% | 0.96% |
| 原 ML | base_ml=100% | 2.80% | -0.87% | 2.78% |
| 向量 ML | vector_ml=100% | 6.17% | 6.39% | 11.33% |
| 规则优先最优 | rule=75%, base_ml=25% | 5.52% | 3.50% | 1.92% |
| 规则优先含向量 | rule=50%, base_ml=25%, vector_ml=25% | 4.13% | 3.28% | 4.95% |

逐年看，规则优先含向量在 2021、2022、2023、2025 年为正超额，但 2024 年明显拖累，Top-K 超额为 `-23.00%`。这说明向量层对语义主线识别有帮助，但在风格切换年份仍可能放大错误主题。

## 2026 观察名单

无约束向量框架的 2026 Top 10 偏向数字经济、AI、云计算、科技创新和新能源：

1. `931469.CSI` 云计算50
2. `931470.CSI` SHS云计算
3. `931643.CSI` 科创创业50
4. `931487.CSI` SHS人工智能50
5. `930851.CSI` 云计算
6. `930598.CSI` 稀土产业
7. `931798.CSI` 光伏龙头30
8. `931406.CSI` 5G50
9. `000941.CSI` 新能源
10. `931160.CSI` 通信设备

这份名单只能作为语义热度和历史相似事件参考，不建议直接替代规则主框架持仓。

## 采用建议

当前主框架：继续使用 `rule=75% + base_ml=25%`。

向量层使用方式：

- 作为二级确认：当规则主框架和向量 ML 同时指向同一主题时，提高主题置信度。
- 作为风险提示：当规则高分但向量新颖度低、重复密度高、相似历史收益差时，降低追高权重。
- 作为候选扩展：对规则未覆盖但向量 ML 排名靠前的主题，进入观察池，不直接进入主仓。

下一步调优重点：

- 把 2024 年误判单独做归因，检查是否由风格切换、主题映射或相似历史收益特征导致。
- 将真正本地 embedding 模型接入同一特征接口，比较 hashed n-gram 与 embedding 的差异。
- 增加组合层约束：行业相关性去重、主题拥挤度惩罚、年度换手惩罚。
- 等历史新闻 backfill 更完整后，扩大验证年份，再决定是否把向量权重从观察层提升到主框架。
