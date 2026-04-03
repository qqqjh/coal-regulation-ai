import requests
import time
import json

task_id = "66cc5341-1014-4ac4-91c5-c53b8d47521c"
url = f"http://localhost:8000/api/review/result/{task_id}"

print(f"Checking task {task_id}...")

for i in range(30):  # Check for up to 2.5 minutes
    time.sleep(5)
    response = requests.get(url)

    if response.status_code == 200:
        result = response.json()
        status = result.get('status', '')
        progress = result.get('progress', 0)
        current_agent = result.get('current_agent', '')

        print(f"[{i+1}] Status: {status}, Progress: {progress}%, Agent: {current_agent}")

        if status == 'completed':
            print("\n=== Analysis Completed! ===")

            review_result = result.get('result', {})
            print(f"Total changes: {review_result.get('total_changes', 0)}")
            print(f"Typo count: {review_result.get('typo_count', 0)}")
            print(f"Fluency count: {review_result.get('fluency_count', 0)}")
            print(f"Duplicate count: {review_result.get('duplicate_count', 0)}")
            print(f"Compliance count: {review_result.get('compliance_count', 0)}")

            print("\n=== Changes ===")
            all_changes = review_result.get('all_changes', [])
            for idx, change in enumerate(all_changes[:5], 1):
                print(f"\n{idx}. Type: {change.get('type', 'unknown')}")
                if 'original' in change:
                    print(f"   Original: {change['original'][:80]}")
                if 'corrected' in change:
                    print(f"   Corrected: {change['corrected'][:80]}")
                if 'improved' in change:
                    print(f"   Improved: {change['improved'][:80]}")

            if len(all_changes) > 5:
                print(f"\n... and {len(all_changes) - 5} more changes")

            # Save full result
            with open('review_result.json', 'w', encoding='utf-8') as f:
                json.dump(review_result, f, ensure_ascii=False, indent=2)
            print("\nFull result saved to review_result.json")

            break

        elif status == 'failed':
            print(f"\nAnalysis FAILED: {result.get('error', 'Unknown error')}")
            break
    else:
        print(f"[{i+1}] Failed to get result: {response.status_code}")
else:
    print("\nTimeout: Analysis took too long")
