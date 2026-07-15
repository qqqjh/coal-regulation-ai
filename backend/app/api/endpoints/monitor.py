from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.schemas import MonitorStats
from app.services.monitor_service import monitor_service
from app.db.database import get_db
from datetime import datetime, timedelta
from typing import Optional

router = APIRouter()

@router.get("/stats")
async def get_monitor_stats(
    start_time: Optional[str] = Query(None, description="开始时间 ISO格式"),
    end_time: Optional[str] = Query(None, description="结束时间 ISO格式"),
    module: Optional[str] = Query(None, description="模块名称"),
    db: AsyncSession = Depends(get_db)
):
    """获取监控统计数据"""
    # 解析时间
    start_dt = datetime.fromisoformat(start_time) if start_time else None
    end_dt = datetime.fromisoformat(end_time) if end_time else None

    stats = await monitor_service.get_stats(
        db=db,
        start_time=start_dt,
        end_time=end_dt,
        module=module
    )

    return stats

@router.get("/trend")
async def get_trend_data(
    hours: int = Query(24, description="统计的小时数"),
    module: Optional[str] = Query(None, description="模块名称"),
    db: AsyncSession = Depends(get_db)
):
    """获取调用趋势数据"""
    trend_data = await monitor_service.get_trend_data(
        db=db,
        hours=hours,
        module=module
    )

    return {"data": trend_data}

@router.get("/distribution")
async def get_module_distribution(
    start_time: Optional[str] = Query(None, description="开始时间 ISO格式"),
    end_time: Optional[str] = Query(None, description="结束时间 ISO格式"),
    db: AsyncSession = Depends(get_db)
):
    """获取模块使用分布"""
    start_dt = datetime.fromisoformat(start_time) if start_time else None
    end_dt = datetime.fromisoformat(end_time) if end_time else None

    distribution = await monitor_service.get_module_distribution(
        db=db,
        start_time=start_dt,
        end_time=end_dt
    )

    return {"data": distribution}

@router.get("/modules")
async def get_module_usage(
    start_time: Optional[str] = Query(None, description="开始时间 ISO格式"),
    end_time: Optional[str] = Query(None, description="结束时间 ISO格式"),
    db: AsyncSession = Depends(get_db)
):
    """获取按模块汇总的调用、Token、成本和耗时"""
    start_dt = datetime.fromisoformat(start_time) if start_time else None
    end_dt = datetime.fromisoformat(end_time) if end_time else None

    usage = await monitor_service.get_module_usage(
        db=db,
        start_time=start_dt,
        end_time=end_dt
    )

    return {"data": usage}

@router.get("/logs")
async def get_recent_logs(
    limit: int = Query(50, description="返回日志数量"),
    module: Optional[str] = Query(None, description="模块名称"),
    status: Optional[str] = Query(None, description="状态"),
    db: AsyncSession = Depends(get_db)
):
    """获取最近的日志记录"""
    logs = await monitor_service.get_recent_logs(
        db=db,
        limit=limit,
        module=module,
        status=status
    )

    return {"logs": logs, "total": len(logs)}
