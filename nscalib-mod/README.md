# NSCalibDamage —— 泰坦陨落 2 内存标定辅助模组

一个 Northstar 服务端模组：**按快捷键对自己的泰坦施加固定伤害，再按一次停止**。
用途是配合 Cheat Engine 定位「泰坦当前血量」等内存字段（插件 `ttf2_coyote` 的必需配置）。

## 安装

把 `NSCalibDamage` 整个文件夹复制到 Northstar 的模组目录：

```
D:\Steam\steamapps\common\Titanfall2\R2Northstar\mods\NSCalibDamage\
    mod.json
    mod\scripts\vscripts\ns_calib_damage.nut
```

然后在同目录的 `R2Northstar\enabledmods.json` 里加上一行（或进游戏在 Northstar 的模组菜单里启用）：

```json
{
    "Northstar.Client": true,
    "Northstar.CustomServers": true,
    "Northstar.Custom": true,
    "NSCalibDamage": true
}
```

> 私服用的是哪个 profile，就装到哪个 profile 的 `mods\` 下。看
> `Titanfall2\ns_startup_args.txt` 里的 `-profile=xxx`：`R2Northstar` 是标准 Northstar 环境。

## 绑定快捷键

进游戏后打开控制台（设置里开启开发者控制台），输入一次即可持久保存：

```
bind F6 "ns_dmg"          // F6 = 开始/停止持续伤害（这就是要的那个开关）
bind F7 "ns_dmg_doom"     // F7 = 强制进入濒死
bind F8 "ns_dmg_once"     // F8 = 单次伤害（精确 Δ 用）
```

## 命令

| 命令 | 作用 |
|---|---|
| `ns_dmg` | **开始/停止**持续伤害（快捷键绑这个） |
| `ns_dmg_once` | 立即造成一次 `amount` 的伤害 |
| `ns_dmg_amount <n>` | 每次伤害量，默认 100 |
| `ns_dmg_interval <s>` | 每跳间隔秒数，默认 0.5（最小 0.05） |
| `ns_dmg_hp <n>` | 把血量**精确降到** n（只支持向下；回血请用真实电池） |
| `ns_dmg_doom` | 强制触发濒死（黑黄条） |
| `ns_dmg_max <n>` | 单次运行最多跳数，默认 0 = 不限 |
| `ns_dmg_stop_on_doom` | 濒死时自动停止：默认**开** |
| `ns_dmg_ticklog` | 每跳日志开关，默认开 |
| `ns_dmg_status` | 打印当前设置与泰坦血量 |

## 每次伤害的日志长这样

```
[NSCalib] -100  7432 -> 7332
```

用它可以和 CE 里的候选值**逐跳对齐**：日志里 `->` 后面的数字就是泰坦真实血量，
CE 里哪个候选同步变成这个值，哪个就是血量字段。

## 推荐用法（配合 CE）

1. 进私人局/边境防御，**坐进泰坦**，CE 附加 **`NorthstarLauncher.exe`**（新版 Northstar 把引擎加载进启动器进程，不存在 `Titanfall2.exe`）；
2. Value Type = 4 Bytes，Scan Type = `Value between...`，填 `100` ~ `20000` → 首次扫描；
3. 按 F6 开始掉血 → CE `Decreased value` → Next Scan；重复几轮；
4. 候选到几十个后，用 **`ns_dmg_hp 5000`**（或已知的目标值）造一次**精确变化**，
   只保留「恰好一步变到 5000」的那个候选；
5. 找到了就用 CE 做指针扫描 + 重启验证，把链填进插件配置。

## 安全性

- 只影响**你自己**的泰坦，不改游戏文件、不改内存里的其它数据；
- 每次伤害前会自动把**护盾清零**，保证伤害落在本体血量上（否则护盾会吃掉这次伤害，
  你会以为「掉血事件没生效」）；
- 濒死时默认自动停止，避免手滑把泰坦打爆；
- `ns_dmg_max` 可限制单次运行的跳数；
- 所有命令受 `ns_calib_allow`（默认 1）或 `sv_cheats` 约束，改成 0 即禁用：
  `ns_calib_allow 0`；
- 这是**服务端模组**，只在加载它的服务器上生效（你自己开的局），不会影响别人的服务器。

## 已验证的 API 依据

代码里的每个调用都对照 Northstar 自带脚本确认过，不是猜的：

| 调用 | 出处 |
|---|---|
| `AddClientCommandCallback( "名字", 回调 )` | `_northstar_cheatcommands.nut`（`noclip` / `demigod` 等） |
| `player.GetTitanSoul()` / `soul.SetShieldHealth()` | `_weapon_utility.nut`、`_arc_cannon.nut` |
| `ent.TakeDamage( dmg, attacker, inflictor, { damageSourceId=…, scriptType=… } )` | `_ai_superspectre.nut`、`_utility.gnut` |
| `GetDoomedState( titan )`、`soul.SetShieldHealth(0)` + `TakeDamage( GetHealth(), …, DF_SKIP_DAMAGE_PROT )` | `_utility.gnut` 的 `ClientCommand_DoomTitan`（官方同款做法） |
| `file.` 作用域、`thread` + `wait`、`printt` | 多个 `Northstar.CustomServers` 脚本 |
| `addClientCommandCallback` 的 `ServerCallback.After` 注册点 | `Northstar.CustomServers\mod.json` |
