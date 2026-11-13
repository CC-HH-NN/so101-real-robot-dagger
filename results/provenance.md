# 结果来源与复现边界

公开结果来自私有 SO-101 真机工作区的评测记录。原始录像、运行日志、部署配置、checkpoint、标定文件和训练数据不随仓库发布；这里保留结果的条件、分母和解释边界，避免把展示副本误读为完整复现包。

| 文件 | 来源与口径 | 公开复现状态 |
| --- | --- | --- |
| `fixed_point.csv` | 五种策略在同一固定玩偶/篮子位置下的人工评分汇总，共 50 个已评分回合。 | 结果表公开；真机配置、权重和原始日志未公开。 |
| `generalization.csv` | π0.5 Training-Time RTC + DAgger 前后两种位置变化的人工评分：前移 3 cm、侧移 6 cm，各 10 回合。 | 结果表公开；位置摆放记录和完整录像未公开。 |
| `training_loss.csv` + `assets/figures/pi-training-curves.png` | π0 和 π0.5 每 200 步的训练 loss、梯度范数和学习率快照；没有 validation loss 记录。 | 训练曲线和数值快照公开；完整训练环境和 checkpoint 未公开。 |
| `dagger_summary.csv` | DAgger 完整轨迹数、人工纠正轨迹数和逐帧 `intervention` 标记口径。 | 摘要公开；完整数据集未公开。 |
| `rtc_runtime_comparison.csv` + `assets/figures/rtc-runtime-comparison.png` | 同一类被动真机时延探针下的预测年龄统计；各策略为独立短时运行。 | 描述性运行证据；严格 RTC 消融配对仍待补齐。 |
| `rtc_latency_distribution.csv` + `assets/figures/rtc-latency-distribution.png` | 三次连续 π0.5 Training-Time RTC step-1000 运行的官方结果分布。 | 真实运行时延分布；不含任务成功率结论。 |
| `rtc_action_trajectory.csv` + `assets/figures/rtc-action-trajectory.png` | 同一记录观测序列上的 20k 与 DAgger +1k 离线动作回放。 | 行为诊断；未向机械臂发送动作。 |
| `rtc_episode_metrics.csv` + `rtc_comparison_summary.csv` | 从普通 π0.5 Inference-Time RTC 与 π0.5 Training-Time RTC 各 10 回合固定点位真机日志中提取帧年龄、请求往返、服务计算、动作块接收、队列余量和动作连续性指标。 | 逐回合和条件汇总公开；两组不是严格配对消融，不能单独作为 RTC 因果结论。 |

## 结果解释

- 成功率分母是已完成评分的任务回合；技术中止、相机时效和硬件通信问题单独记录，不自动改写成模型失败。
- 固定点位结果用于比较执行链上的不同策略，不代表任意场景泛化。
- 位置变化结果只支持 π0.5 Training-Time RTC + DAgger 的两种变化，不能外推到 ACT、Diffusion Policy 或任意位置/任意物体。
- DAgger 完整轨迹用于追溯和展示；训练使用连续有效的人工纠正窗口。
- RTC 动作连续性指标来自实际下发目标的时间序列；离线回放结果不作为真机平滑性证据。

## 内部重现入口

真实复现命令依赖私有的 LeRobot 环境、远程推理服务、模型文件、相机配置、机械臂标定和运行日志，因此不在公开仓库中提供伪造的“一键复现”命令。内部运行记录绑定到项目工作区中的具体启动配置和 checkpoint；公开仓库的 Git 提交只对应本文档、结果摘要和展示素材。
