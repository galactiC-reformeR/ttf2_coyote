untyped

global function NSCalibDamage_Init

// ---------------------------------------------------------------------------
// NSCalibDamage —— 内存标定辅助：对自己的泰坦施加可控、可停止的固定伤害
//
// 用途：配合 Cheat Engine 定位「泰坦当前血量」等内存字段。
//   1) 坐进泰坦，CE 附加 Titanfall2.exe，按「未知初始值 / 数值下降」收窄；
//   2) 按快捷键开始持续掉血，反复收窄候选；
//   3) 需要精确 Δ 匹配时，用 ns_dmg_once 造「恰好 -amount」的单次变化。
//
// 安全性：只影响本地玩家自己的泰坦；不改游戏文件；随服务器关闭即失效。
//   所有命令都受 ns_calib_allow（默认 1）或 sv_cheats 约束。
//
// 参考实现来源：Northstar.CustomServers 自带的 ClientCommand_DoomTitan
//   （soul.SetShieldHealth(0) + TakeDamage(..., { scriptType = DF_SKIP_DAMAGE_PROT })）。
// ---------------------------------------------------------------------------

struct {
	bool  running     = false
	int   amount      = 100
	float interval    = 0.5
	int   maxTicks    = 0
	int   ticksDone   = 0
	bool  stopOnDoom  = true
	bool  tickLog     = true
} file

const float NSCALIB_MIN_INTERVAL = 0.05

void function NSCalibDamage_Init()
{
	file.amount     = NSCalib_ConVarInt( "ns_calib_amount", 100 )
	file.interval   = max( NSCALIB_MIN_INTERVAL, NSCalib_ConVarFloat( "ns_calib_interval", 0.5 ) )
	file.maxTicks   = NSCalib_ConVarInt( "ns_calib_max_ticks", 0 )
	file.stopOnDoom = NSCalib_ConVarInt( "ns_calib_stop_on_doom", 1 ) != 0
	file.tickLog    = NSCalib_ConVarInt( "ns_calib_tick_log", 1 ) != 0

	AddClientCommandCallback( "ns_dmg",                NSCalib_CC_Toggle )
	AddClientCommandCallback( "ns_dmg_once",           NSCalib_CC_Once )
	AddClientCommandCallback( "ns_dmg_amount",         NSCalib_CC_Amount )
	AddClientCommandCallback( "ns_dmg_interval",       NSCalib_CC_Interval )
	AddClientCommandCallback( "ns_dmg_hp",             NSCalib_CC_SetHp )
	AddClientCommandCallback( "ns_dmg_doom",           NSCalib_CC_Doom )
	AddClientCommandCallback( "ns_dmg_max",            NSCalib_CC_Max )
	AddClientCommandCallback( "ns_dmg_ticklog",        NSCalib_CC_TickLog )
	AddClientCommandCallback( "ns_dmg_stop_on_doom",   NSCalib_CC_StopOnDoom )
	AddClientCommandCallback( "ns_dmg_status",         NSCalib_CC_Status )

	printt( "[NSCalib] 已加载：默认每次 -" + file.amount + "，间隔 " + file.interval + "s" )
	printt( "[NSCalib] 快捷键：控制台输入  bind F6 \"ns_dmg\"   之后按 F6 即可开始/停止" )
}

// ---------------------------------------------------------------------------
// 辅助
// ---------------------------------------------------------------------------

int function NSCalib_ConVarInt( string name, int fallback )
{
	string raw = GetConVarString( name )
	if ( raw == "" )
		return fallback
	return raw.tointeger()
}

float function NSCalib_ConVarFloat( string name, float fallback )
{
	string raw = GetConVarString( name )
	if ( raw == "" )
		return fallback
	return raw.tofloat()
}

bool function NSCalib_Allowed()
{
	return GetConVarBool( "sv_cheats" ) || GetConVarBool( "ns_calib_allow" )
}

// 返回玩家当前可操作的泰坦实体（在泰坦内时 player 自身就是泰坦）
entity function NSCalib_GetTitan( entity player )
{
	if ( !IsValid( player ) )
		return null
	if ( player.IsTitan() )
		return player

	entity pet = player.GetPetTitan()
	if ( IsValid( pet ) )
		return pet

	return null
}

// 对泰坦本体造成固定伤害。先清空护盾，保证伤害落在血量上（否则护盾会吃掉这次伤害）
void function NSCalib_ApplyDamage( entity player, int amount )
{
	entity titan = NSCalib_GetTitan( player )
	if ( !IsValid( titan ) || !IsAlive( titan ) )
		return

	entity soul = titan.GetTitanSoul()
	if ( IsValid( soul ) )
		soul.SetShieldHealth( 0 )

	int before = titan.GetHealth()
	titan.TakeDamage( amount, null, null, { damageSourceId = damagedef_suicide, scriptType = DF_SKIP_DAMAGE_PROT } )
	int after = IsAlive( titan ) ? titan.GetHealth() : 0

	if ( file.tickLog )
		printt( "[NSCalib] -" + amount + "  " + before + " -> " + after )
}

// ---------------------------------------------------------------------------
// 持续伤害循环
// ---------------------------------------------------------------------------

void function NSCalib_Loop( entity player )
{
	file.ticksDone = 0
	printt( "[NSCalib] 持续伤害开始：每 " + file.interval + "s  -" + file.amount )

	while ( file.running )
	{
		if ( !IsValid( player ) || !IsAlive( player ) )
		{
			printt( "[NSCalib] 玩家已死亡或失效，停止" )
			break
		}

		entity titan = NSCalib_GetTitan( player )
		if ( !IsValid( titan ) || !IsAlive( titan ) )
		{
			printt( "[NSCalib] 找不到存活的泰坦（是否不在泰坦内？），停止" )
			break
		}

		if ( file.stopOnDoom && GetDoomedState( titan ) )
		{
			printt( "[NSCalib] 泰坦已进入濒死，自动停止（ns_dmg_stop_on_doom 0 可关闭此保护）" )
			break
		}

		NSCalib_ApplyDamage( player, file.amount )
		file.ticksDone++

		if ( file.maxTicks > 0 && file.ticksDone >= file.maxTicks )
		{
			printt( "[NSCalib] 已达本次上限 " + file.maxTicks + " 次，停止" )
			break
		}

		wait file.interval
	}

	file.running = false
	printt( "[NSCalib] 持续伤害已停止（本次共 " + file.ticksDone + " 次）" )
}

// ---------------------------------------------------------------------------
// 命令
// ---------------------------------------------------------------------------

bool function NSCalib_CC_Toggle( entity player, array<string> args )
{
	if ( !NSCalib_Allowed() )
		return true

	if ( file.running )
	{
		file.running = false          // 循环在下一个 tick 自行退出
		printt( "[NSCalib] 收到停止指令" )
	}
	else
	{
		file.running = true
		thread NSCalib_Loop( player )
	}
	return true
}

bool function NSCalib_CC_Once( entity player, array<string> args )
{
	if ( !NSCalib_Allowed() )
		return true
	NSCalib_ApplyDamage( player, file.amount )
	return true
}

bool function NSCalib_CC_Amount( entity player, array<string> args )
{
	if ( !NSCalib_Allowed() )
		return true
	if ( args.len() < 1 )
	{
		printt( "[NSCalib] 当前每次伤害 = " + file.amount + "；用法：ns_dmg_amount <数值>" )
		return true
	}
	file.amount = maxint( 1, args[0].tointeger() )
	printt( "[NSCalib] 每次伤害已设为 " + file.amount )
	return true
}

bool function NSCalib_CC_Interval( entity player, array<string> args )
{
	if ( !NSCalib_Allowed() )
		return true
	if ( args.len() < 1 )
	{
		printt( "[NSCalib] 当前间隔 = " + file.interval + "s；用法：ns_dmg_interval <秒>" )
		return true
	}
	file.interval = max( NSCALIB_MIN_INTERVAL, args[0].tofloat() )
	printt( "[NSCalib] 间隔已设为 " + file.interval + "s" )
	return true
}

bool function NSCalib_CC_Max( entity player, array<string> args )
{
	if ( !NSCalib_Allowed() )
		return true
	if ( args.len() < 1 )
	{
		printt( "[NSCalib] 当前单次运行上限 = " + file.maxTicks + "（0 = 不限）；用法：ns_dmg_max <次数>" )
		return true
	}
	file.maxTicks = maxint( 0, args[0].tointeger() )
	printt( "[NSCalib] 单次运行上限已设为 " + file.maxTicks + " 次" )
	return true
}

bool function NSCalib_CC_TickLog( entity player, array<string> args )
{
	if ( !NSCalib_Allowed() )
		return true
	file.tickLog = !file.tickLog
	printt( "[NSCalib] 每跳日志：" + ( file.tickLog ? "开" : "关" ) )
	return true
}

bool function NSCalib_CC_StopOnDoom( entity player, array<string> args )
{
	if ( !NSCalib_Allowed() )
		return true
	file.stopOnDoom = !file.stopOnDoom
	printt( "[NSCalib] 濒死自动停止：" + ( file.stopOnDoom ? "开" : "关" ) )
	return true
}

// 把血量精确降到目标值（只支持向下调整；向上恢复请用真实电池）
bool function NSCalib_CC_SetHp( entity player, array<string> args )
{
	if ( !NSCalib_Allowed() )
		return true
	if ( args.len() < 1 )
	{
		printt( "[NSCalib] 用法：ns_dmg_hp <目标血量>" )
		return true
	}

	entity titan = NSCalib_GetTitan( player )
	if ( !IsValid( titan ) || !IsAlive( titan ) )
	{
		printt( "[NSCalib] 当前没有可操作的泰坦" )
		return true
	}

	int target = maxint( 1, args[0].tointeger() )
	int diff = titan.GetHealth() - target
	if ( diff <= 0 )
	{
		printt( "[NSCalib] 当前血量 " + titan.GetHealth() + " 已不高于目标 " + target + "，未做任何事" )
		return true
	}

	NSCalib_ApplyDamage( player, diff )
	return true
}

// 强制进入濒死（与 Northstar 自带 DoomTitan 命令同一套做法）
bool function NSCalib_CC_Doom( entity player, array<string> args )
{
	if ( !NSCalib_Allowed() )
		return true

	entity titan = NSCalib_GetTitan( player )
	if ( !IsValid( titan ) || !IsAlive( titan ) )
	{
		printt( "[NSCalib] 当前没有可操作的泰坦" )
		return true
	}
	if ( GetDoomedState( titan ) )
	{
		printt( "[NSCalib] 泰坦已经在濒死状态" )
		return true
	}

	entity soul = titan.GetTitanSoul()
	if ( IsValid( soul ) )
		soul.SetShieldHealth( 0 )

	titan.TakeDamage( titan.GetHealth(), null, null, { damageSourceId = damagedef_suicide, scriptType = DF_SKIP_DAMAGE_PROT } )
	printt( "[NSCalib] 已强制触发濒死" )
	return true
}

bool function NSCalib_CC_Status( entity player, array<string> args )
{
	entity titan = NSCalib_GetTitan( player )

	string line = "[NSCalib] 状态："
	line += ( file.running ? "运行中" : "已停止" )
	line += "  每跳 -" + file.amount
	line += "  间隔 " + file.interval + "s"
	line += "  上限 " + file.maxTicks
	line += "  濒死自动停 " + ( file.stopOnDoom ? "开" : "关" )
	line += "  日志 " + ( file.tickLog ? "开" : "关" )

	if ( IsValid( titan ) && IsAlive( titan ) )
	{
		line += "   |  泰坦血量 " + titan.GetHealth()
		line += " / 最大 " + titan.GetMaxHealth()
		line += "  濒死 " + ( GetDoomedState( titan ) ? "是" : "否" )
	}
	else
	{
		line += "   |  当前不在泰坦内"
	}

	printt( line )
	return true
}
