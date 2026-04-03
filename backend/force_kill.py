"""强制终止所有在8000端口的进程"""
import psutil
import sys

killed = []
for proc in psutil.process_iter(['pid', 'name']):
    try:
        connections = proc.connections()
        if connections:
            for conn in connections:
                if hasattr(conn, 'laddr') and conn.laddr.port == 8000:
                    pid = proc.pid
                    name = proc.name()
                    print(f"Killing process {pid} ({name})")
                    proc.kill()
                    killed.append(pid)
                    break
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, AttributeError):
        pass

if killed:
    print(f"\nKilled {len(killed)} processes: {killed}")
else:
    print("No processes found on port 8000")
