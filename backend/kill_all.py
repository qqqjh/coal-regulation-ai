import os
import subprocess

# Get all processes on port 8000
result = subprocess.run(['netstat', '-ano'], capture_output=True, text=True)
lines = result.stdout.split('\n')

pids = set()
for line in lines:
    if ':8000' in line and 'LISTENING' in line:
        parts = line.split()
        if parts:
            pid = parts[-1]
            if pid.isdigit():
                pids.add(pid)

print(f"Found {len(pids)} processes on port 8000:")
for pid in pids:
    print(f"  PID: {pid}")
    try:
        os.system(f'taskkill /F /PID {pid}')
    except:
        pass

print("\nAll processes killed.")
