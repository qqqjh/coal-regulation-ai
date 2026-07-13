from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_
from app.models.database import MonitorLog
from datetime import datetime, timedelta
from typing import Optional, Dict, List
import json

class MonitorService:
    """监控服务"""

    async def log_operation(
        self,
        db: AsyncSession,
        module: str,
        operation: str,
        status: str,
        tokens: int = 0,
        latency: float = 0.0,
        cost: float = 0.0,
        error_msg: Optional[str] = None,
        metadata: Optional[Dict] = None
    ):
        """记录操作日志"""
        log = MonitorLog(
            module=module,
            operation=operation,
            status=status,
            tokens=tokens,
            latency=latency,
            cost=cost,
            error_msg=error_msg,
            extra_data=json.dumps(metadata, ensure_ascii=False) if metadata else None,
            timestamp=datetime.utcnow()
        )
        db.add(log)
        await db.commit()
        return log

    async def get_stats(
        self,
        db: AsyncSession,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        module: Optional[str] = None
    ) -> Dict:
        """获取统计数据"""
        # 默认统计最近24小时
        if not start_time:
            start_time = datetime.utcnow() - timedelta(days=1)
        if not end_time:
            end_time = datetime.utcnow()

        # 构建查询条件
        conditions = [
            MonitorLog.timestamp >= start_time,
            MonitorLog.timestamp <= end_time
        ]
        if module:
            conditions.append(MonitorLog.module == module)

        # 总调用次数
        total_calls_query = select(func.count(MonitorLog.id)).where(and_(*conditions))
        result = await db.execute(total_calls_query)
        total_calls = result.scalar() or 0

        # 总Token数
        total_tokens_query = select(func.sum(MonitorLog.tokens)).where(and_(*conditions))
        result = await db.execute(total_tokens_query)
        total_tokens = result.scalar() or 0

        # 平均延迟
        avg_latency_query = select(func.avg(MonitorLog.latency)).where(and_(*conditions))
        result = await db.execute(avg_latency_query)
        avg_latency = result.scalar() or 0.0

        # 总成本
        total_cost_query = select(func.sum(MonitorLog.cost)).where(and_(*conditions))
        result = await db.execute(total_cost_query)
        total_cost = result.scalar() or 0.0

        # 错误率
        failed_calls_query = select(func.count(MonitorLog.id)).where(
            and_(*conditions, MonitorLog.status == "failed")
        )
        result = await db.execute(failed_calls_query)
        failed_calls = result.scalar() or 0
        error_rate = (failed_calls / total_calls * 100) if total_calls > 0 else 0

        return {
            "total_calls": total_calls,
            "total_tokens": int(total_tokens),
            "avg_latency": round(avg_latency, 3),
            "total_cost": round(total_cost, 4),
            "error_rate": round(error_rate, 2),
            "failed_calls": failed_calls
        }

    async def get_trend_data(
        self,
        db: AsyncSession,
        hours: int = 24,
        module: Optional[str] = None
    ) -> List[Dict]:
        """获取趋势数据"""
        start_time = datetime.utcnow() - timedelta(hours=hours)

        conditions = [MonitorLog.timestamp >= start_time]
        if module:
            conditions.append(MonitorLog.module == module)

        # 按小时聚合
        query = select(
            func.strftime('%Y-%m-%d %H:00', MonitorLog.timestamp).label('hour'),
            func.count(MonitorLog.id).label('calls'),
            func.sum(MonitorLog.tokens).label('tokens')
        ).where(and_(*conditions)).group_by('hour').order_by('hour')

        result = await db.execute(query)
        rows = result.fetchall()

        return [
            {
                "time": row.hour,
                "calls": row.calls,
                "tokens": row.tokens or 0
            }
            for row in rows
        ]

    async def get_module_distribution(
        self,
        db: AsyncSession,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None
    ) -> List[Dict]:
        """获取模块使用分布"""
        if not start_time:
            start_time = datetime.utcnow() - timedelta(days=7)
        if not end_time:
            end_time = datetime.utcnow()

        query = select(
            MonitorLog.module,
            func.count(MonitorLog.id).label('count')
        ).where(
            and_(
                MonitorLog.timestamp >= start_time,
                MonitorLog.timestamp <= end_time
            )
        ).group_by(MonitorLog.module)

        result = await db.execute(query)
        rows = result.fetchall()

        return [
            {
                "name": row.module,
                "value": row.count
            }
            for row in rows
        ]

    async def get_module_usage(
        self,
        db: AsyncSession,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None
    ) -> List[Dict]:
        """按模块汇总调用、Token、成本和耗时"""
        if not start_time:
            start_time = datetime.utcnow() - timedelta(days=7)
        if not end_time:
            end_time = datetime.utcnow()

        query = select(
            MonitorLog.module,
            func.count(MonitorLog.id).label("calls"),
            func.sum(MonitorLog.tokens).label("tokens"),
            func.sum(MonitorLog.cost).label("cost"),
            func.avg(MonitorLog.latency).label("avg_latency"),
        ).where(
            and_(
                MonitorLog.timestamp >= start_time,
                MonitorLog.timestamp <= end_time
            )
        ).group_by(MonitorLog.module).order_by(func.sum(MonitorLog.tokens).desc())

        result = await db.execute(query)
        rows = result.fetchall()

        return [
            {
                "module": row.module,
                "calls": int(row.calls or 0),
                "tokens": int(row.tokens or 0),
                "cost": round(row.cost or 0.0, 4),
                "avg_latency": round(row.avg_latency or 0.0, 3),
            }
            for row in rows
        ]

    async def get_recent_logs(
        self,
        db: AsyncSession,
        limit: int = 50,
        module: Optional[str] = None,
        status: Optional[str] = None
    ) -> List[Dict]:
        """获取最近日志"""
        conditions = []
        if module:
            conditions.append(MonitorLog.module == module)
        if status:
            conditions.append(MonitorLog.status == status)

        query = select(MonitorLog).order_by(MonitorLog.timestamp.desc()).limit(limit)

        if conditions:
            query = query.where(and_(*conditions))

        result = await db.execute(query)
        logs = result.scalars().all()

        return [
            {
                "id": log.id,
                "module": log.module,
                "operation": log.operation,
                "status": log.status,
                "tokens": log.tokens,
                "latency": log.latency,
                "cost": log.cost,
                "timestamp": log.timestamp.isoformat(),
                "error_msg": log.error_msg
            }
            for log in logs
        ]

monitor_service = MonitorService()
