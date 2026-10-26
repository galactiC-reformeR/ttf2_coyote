# 引擎契约（语言中立）

本文是引擎与宿主之间的接口规定。**引擎是纯逻辑**：不碰进程内存、不碰设备、不读系统时钟，因此可以用采样轨回放做完整回归测试。

---

## 1. 端口（Port）

```
GameSource                       # 内存只读轮询实现；未来可换 Northstar 实现
    init(cfg) -> void
    read()    -> Sample          # 失败时返回 readOk=false，绝不返回 0 血量

DeviceSink                       # 郊狼下发
    readLimits()            -> Limits | Unavailable
    setIntensity(ch, v)     -> void          # ch ∈ {A,B}
    pushWaveform(ch, wave)  -> void          # 持续推送；停止推送即停止输出
    stopChannel(ch)         -> void          # 立即中断（强度置 0 并停止推送）
    onLimitsChanged(cb)     -> void          # 可选；不支持时必须能轮询

HostApi                          # DGHub 宿主
    getConfig()             -> Config        # 条目强度/波形/时长/开关的当前用户值
    onConfigChanged(cb)     -> void
    log(level, line)        -> void
    notify(userText)        -> void          # 可选：面板提示（如"通道已停用"）
```

## 2. 数据结构

```
Sample = {
    tick:      int64,      # 单调毫秒；由调用方提供，引擎不读系统时钟
    readOk:    bool,       # false 表示本帧读取失败
    epoch:     int,        # 泰坦生命周期 id，每次上机递增
    inTitan:   bool,
    alive:     bool,
    health:    int,
    maxHealth: int,
    doomed:    bool,
    m20:       bool,       # 脚本视图/相机状态（处决动画）
    m25:       bool        # 死亡界面已出现
}

Limits = { aOut, aAbs, bOut, bAbs: int | null }      # null = 不可读

Decision = {
    slot:   Slot,                        # 想输出什么
    cfg:    int,                         # 该 slot 的配置强度（未限幅）
    eff:    int,                         # 实际下发强度
    clamp:  { kind: "none"|"out"|"abs", limit: int },   # 被哪个上限削顶
    reason: Reason
}

Slot   = A_70 | A_50 | A_30 | A_DOOM | A_NONE | E_DEATH | B_HIT | B_NONE
Reason = init | idle | tier-enter | recover-refresh | cfg-change | hit | pulse-end
       | death-screen | anim-mute | switch-off | death-window | estop | device-lost
       | ch-disabled | no-titan | dead | invalid | clamp

# reason 语义补充（写测试向量时确定）：
#   init       会话第一帧的基准行，两通道一律用 init，不论 slot 是什么
#   idle       A_NONE / B_NONE 的默认原因（没有更优先的阻断原因时）
#   cfg-change 用户改了配置但生效强度没变（例如 cfg 60→40 而上限恰好是 40）—— 必须记日志，但不下发
#   tier-leave 已废弃：离开档位体现为上一行与这一行的差别，不需要单独原因
```

## 3. 引擎状态

```
{
  epoch,                     # 当前纪元
  baseline,                  # 血量基线（恢复时重设、受击时下移）
  tier,                      # 带滞回的当前档位
  muteGate:   { released: bool, on: bool },
  deathWindow:{ active: bool, epoch: int, startTick: int, notInTitanSince: int|null },
  hits:       { lastHitTick: int, mergedCount: int, mergedTotal: int },
  e:          { active: bool, endTick: int } | null,
  invalidStreak: int,
  needsRelocate: bool,       # 交由宿主/数据源处理
  outLimit: Limits,
  lastDispatchKey: { A: (slot,eff), B: (slot,eff) },
  lastLogKey:      { A: (slot,cfg,eff,clamp,reason), B: (...) }
}
```

---

## 4. 每帧处理顺序（严格按此顺序）

> ⚠️ 本顺序修正了 `TF2插件设计思路-v9` §3 伪代码里的一处次序问题：v9 把「输出分配」写在「基线维护」之前，导致同帧的受击要在下一帧才触发脉冲。正确顺序是**先检测、后分配**。

```
feed(sample, config, limits, estop, switchB) -> Decision[A], Decision[B]

1. 死亡序列窗口
   if deathWindow.active:
        丢弃本帧（不检测、不重基线、不补发）
        if sample.epoch != deathWindow.epoch        -> 关窗 + 用校验通过的本帧重基线
        elif inTitan == false 持续 >= 500ms          -> 关窗 + 重基线
        elif tick - startTick >= ceilingMs           -> 关窗 + 重基线 + needsRelocate
        输出：A = (A_NONE, 0, reason=death-window)   B 保持 E 的进度（若 E 在跑）
        return

2. 死亡界面上升沿（优先于静音门）
   if m25 由假转真:
        snapshotInTitan = sample.inTitan              # 死亡瞬间快照，必须当帧取得
        muteGate.released = true                      # 粘滞解除，锁到 epoch 改变
        deathWindow = { active: true, epoch, startTick: tick }
        if snapshotInTitan:  e = { active: true, endTick: tick + 5000 }

3. 元组校验（fail-closed）
   Invalid 当且仅当：
        !readOk
        | health < 0 | health > maxHealth | maxHealth <= 0
        | doomed == true && alive == false
        | inTitan == false && 该采样属于泰坦纪元
        | epoch 未变 && |health - baseline| > maxHealth
   if Invalid:
        invalidStreak += 1；连续超阈值 -> needsRelocate = true
        A = (A_NONE, 0, reason=invalid)；B = 若 E 在跑则继续 E，否则 (B_NONE,0,invalid)
        return                                        # 绝不重基线、绝不下移 baseline

4. 静音门（仅在未粘滞解除时逐帧评估）
   if !muteGate.released: muteGate.on = (sample.m20 && sample.doomed)
   if muteGate.on:
        取消在途 B 脉冲（含受击；E 不在此时启动，因为 E 由第 2 步在解除后启动）
        A = (A_NONE, 0, reason=anim-mute)；B = (B_NONE, 0, reason=anim-mute)
        清除 hits 合并计数；return

5. 检测（先检测、后分配）
   if doomed 由假转真:                                # 濒死切换：血量字段语义会突变
        静默重基线（baseline = health），不产生受击
   elif doomed 由真转假:                              # 插电池解除濒死
        baseline = health；tier 立即重算（本帧忽略滞回）
   elif sample.health > baseline:                     # 恢复
        baseline = health；tier 立即重算（本帧忽略滞回）
   elif sample.health < baseline:                     # 受击
        Δ = baseline - health；baseline = health
        if 死亡帧收尾扣减（Δ 近似等于剩余全部血量 && alive 由真转假）-> 不产生受击
        elif tick - hits.lastHitTick >= refractoryMs  -> 新脉冲：hits = {tick, 1, Δ}
        else                                          -> 合并：hits.count++, total += Δ

6. 输出分配（限幅在下一步）
   # 通道 A（电平）
   A.slot = tierSlot(...)                            # 见 §5
   A.cfg  = tierCfg(A.slot)
   # 通道 B（脉冲，优先级 E > 受击）
   if e.active && tick >= e.endTick:
        e = null；B = (B_NONE, 0, reason=pulse-end, dur=5000)
   elif e.active:            B.slot = E_DEATH
   elif !switchB:            B.slot = B_NONE；取消在途受击脉冲；reason = switch-off
   elif hits.count > 0 且脉冲未结束: B.slot = B_HIT
   else:                     B.slot = B_NONE
   # 无档位 / 无脉冲时的默认原因
   if A.slot == A_NONE && 无更优先阻断原因: A.reason = idle
   if B.slot == B_NONE && 非 pulse-end / switch-off: B.reason = idle

7. 限幅（最后一步，只作用于输出）
   for ch in {A,B}:
        limit = min(out, abs)（忽略不可读者）
        if limit == 0:      eff = 0; reason = ch-disabled
        elif cfg > limit:   eff = limit; clamp = {被哪个上限, limit}; reason = clamp
        else:               eff = cfg;  clamp = none

8. 去重与日志
   dispatch 当且仅当 (slot, eff) 变化
   log      当且仅当 (slot, cfg, eff, clamp, reason) 变化
```

### 顺序上的两个要点

- **第 5 步必须在第 6 步之前**，否则受击要等下一帧才出脉冲（8 ms 额外延迟，且不应期边界会错）。
- **第 7 步限幅只在最后**，绝不能回写 `cfg`：否则「配置强度」被污染，日志再也无法回答「我配的是多少」。

---

## 5. 档位判定（整数比较 + 滞回）

```
tierSlot(health, maxHealth, doomed, prevTier, hysteresisPct=2):
    if doomed -> A_DOOM                                  # 濒死优先，绝不用 health==0 判断
    M = maxHealth; h = health
    # 进档用严格下界，退档用 (阈值 + 滞回) —— 避免边界抖动
    if 100*h < 30*M  -> A_30
    elif 100*h < 50*M -> A_50
    elif 100*h < 70*M -> A_70
    else -> A_NONE
    # 滞回：若 prevTier 更强且当前值未超过「阈值 + hysteresisPct」，保持 prevTier
```

配置强度：`A_70=20, A_50=30, A_30=40, A_DOOM=60`；`B_HIT=25`；`E_DEATH=45`；其余为 0。

---

## 6. 优先级链（最终）

```
通道 A：  estop / device-lost  >  death-window  >  anim-mute  >  档位(60/40/30/20)  >  A_NONE
通道 B：  estop / device-lost  >  switch-off（整通道 0，并切断在途 E）  >  anim-mute（取消在途）
                              >  E_DEATH（5 s 独占）  >  B_HIT（200 ms 不应期合并）  >  B_NONE
```

跨通道联动只有三处：`anim-mute`、`estop`、`device-lost`。其余情况两通道完全独立。

---

## 7. 限幅层

```
eff = min(cfg, 通道输出上限, 通道绝对上限)
```

- 单向下压；逐通道独立；只作用于输出；**禁止自动补偿**；
- 上限**降低必须立即生效、禁止平滑**；提高可走爬升时间（默认 0）；
- 上限变化不得重启波形、不得重置脉冲剩余时长；
- 上限 = 0 等价于通道停用（`ch-disabled`）；
- 用户上限**不作为插件配置持久化**，来源只有设备会话（避免双份真相）。

**两种实现位置**：

| 情形 | 实现 | 能否显示"被限幅" |
|---|---|---|
| 上限可读 | 插件内 `min()` | ✅ 可显示、可记录 |
| 上限不可读（`Limits = Unavailable`） | 设备侧削顶；引擎按 `cfg` 处理，`clamp=none` | ❌ 不能；必须在说明里写明「实际强度可能低于配置强度」 |

---

## 8. 日志（唯一出口）

- 所有分支（含输出 0 的全部路径）都必须经过唯一出口产生 `Decision` 后统一记录，禁止在分支里散落写日志；
- **日志键 = `(slot, cfg, eff, clamp, reason)`**（比 v12 增加了 `reason`：让「为什么从 X 变成 Y」也可见）；
- 行格式（含游戏态快照，便于与游戏内现象对照）：

```
[12:35:09.700] ch=A slot=A_DOOM cfg=60 eff=40 clamp=out(40) reason=clamp       hp=0.0% doomed=1 titan=1
[12:35:16.900] ch=B slot=E_DEATH cfg=45 eff=45 clamp=-       reason=death-screen
[12:35:21.900] ch=B slot=B_NONE  cfg=0  eff=0  clamp=-       reason=pulse-end dur=5000ms
```

- 级别：变更日志（默认开）、会话事件（默认开：连接/断连/上限读取值/上限变化/开关/急停）、心跳行（可选，默认关）、原始采样（默认关，开启必须限速并定时自动关）；
- 合并的连发不单独记日志，在 `pulse-end` 行带 `n=` 与 `Δtotal=`。

---

## 9. 不变量（实现必须满足）

1. `20 < 30 < 40 < 60`（按配置值校验；限幅后允许相等，不得报错）。
2. 25% 血量只输出 40，不得叠加。
3. 静音门窗口内不得产生任何脉冲。
4. 处决动画期间 A 与 B 同时为 0。
5. 死亡界面出现前不得输出 E；E 起点 = M25 上升沿 + 当帧 `inTitan` 快照。
6. E 时长恰为 5000 ms；起振必须有一条对应的结束行，其 reason 为 `pulse-end` / `switch-off` / `estop` / `device-lost` 之一，且只有 `pulse-end` 带 `dur=5000ms`（被开关或急停切断时不补发、不重启计时）。
7. 死亡窗口内不产生事件、不重基线、不补发；退出必须用通过校验的样本重基线。
8. 窗口结束以 **epoch 变化** 为第一优先。
9. **读取失败不得被解释为 0**。
10. `Invalid` 帧一律作废；连续 Invalid 触发 `needsRelocate`。
11. 恢复不产生任何脉冲，只重设基线 + 立即重算档位。
12. 受击开关关闭时通道 B 恒为 0（含切断在途 E）。
13. 不应期内多次扣减合并为一次 `B_HIT`。
14. 己方处决敌方（`m20 ∧ !doomed`）不得产生脉冲，也不得误静音。
15. `eff <= 用户上限` 恒成立（上限可读时）。
16. 急停 / 心跳超时 → 两通道归零，优先级高于一切条目。
