# LinkedIn draft: loop engineering under a local inference budget

## Recommended post

I ran a small controlled experiment to answer a question that gets lost in the
agent-loop hype:

**Does a better loop actually solve more tasks, or does it just call the model
more often?**

I built EdgeLoopBench and tested three controller strategies:

- Direct: one model attempt
- Bounded Retry: up to three attempts with public-test feedback
- Goal Skill Loop: a verifiable goal, a fixed five-part verification skill,
  and up to five attempts

I deliberately ran the qualification locally with open-weight models instead
of spending API tokens while tuning the topology. The experiment used Phi-4
Mini 3.8B and Qwen3.5 4B, eight fresh offline Python repair tasks, isolated
hidden tests, pinned model/runtime/controller revisions, identical logical
budget ceilings, and paired task-level analysis.

The result was useful precisely because it was negative:

- Phi-4 Mini: 12.5% verified success for all three strategies
- Qwen3.5 4B: 37.5% verified success for all three strategies
- Qwen's Goal Skill Loop rescued one task—but regressed another
- Net uplift: 0 percentage points
- Goal Skill Loop cost: 3.16× Direct's tokens on Qwen and 6.09× on Phi

My takeaway: **loop engineering is test-time scaling with a control policy, not
a free capability upgrade.** A loop creates value only when the model can turn
visible failure evidence into a better candidate more often than it damages an
already-good one.

The practical evaluation unit should therefore be:

> rescues − regressions, under explicit token and latency cost

—not simply the number of iterations or whether an agent looked busy.

This is an eight-task qualification pilot, not a universal coding benchmark or
a claim that loops never work. The next justified step would be a larger,
disjoint confirmatory suite only after a topology shows positive net rescue.

I published the controller, frozen manifests, task generator, paired
statistics, machine-readable evidence, and a self-contained HTML report here:

`<GitHub repository URL>`
#AIEngineering #LLM #Agents #LocalAI #MachineLearning #Evaluation

## Short Chinese alternative

我做了一個小型但完整控制的 loop engineering 實驗，想回答一個很實際的問題：

**Agent 多跑幾輪，是真的解出更多題，還是只消耗更多 inference？**

我用本機 Phi-4 Mini 3.8B 與 Qwen3.5 4B，比較 Direct、最多三輪的
Bounded Retry，以及「可驗證目標＋固定 verification skill＋最多五輪」的
Goal Skill Loop。8 題離線 Python repair、hidden evaluation 隔離、模型與
controller revision 全部 pin 住，並以同 task paired outcome 分析。

結果：兩個模型的淨 uplift 都是 0。Qwen 的 Goal Skill Loop 救回一題，
但也弄壞一題；token 成本則是 Direct 的 3.16 倍。Phi 沒有任何 rescue，
token 成本為 6.09 倍。

我的結論不是「loop 沒用」，而是：

**Loop engineering 是帶有 control policy 的 test-time scaling，不是免費的能力升級。**

真正應該看的指標是 `rescues − regressions`，並同時揭露 token 與 latency，
而不是只看 agent 跑了幾輪。

完整 controller、實驗配置、paired statistics 與 HTML report：

`<GitHub repository URL>`
