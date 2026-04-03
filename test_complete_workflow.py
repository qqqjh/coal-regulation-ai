"""
测试文档审查完整流程的自动化测试脚本
"""
import requests
import time
import json
from pathlib import Path
import sys
import io

# 设置标准输出编码为UTF-8
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

BASE_URL = "http://localhost:8000/api/review"
TEST_FILE = "backend/data/uploads/test_review.docx"

def test_complete_workflow():
    """测试完整的文档审查工作流程"""

    print("=" * 80)
    print("开始测试文档审查完整流程")
    print("=" * 80)

    # 步骤1: 上传文档
    print("\n[步骤1] 上传测试文档...")

    if not Path(TEST_FILE).exists():
        print(f"错误: 测试文件不存在 {TEST_FILE}")
        return

    with open(TEST_FILE, 'rb') as f:
        files = {'file': ('test_review.docx', f, 'application/vnd.openxmlformats-officedocument.wordprocessingml.document')}
        response = requests.post(f"{BASE_URL}/upload", files=files)

    if response.status_code != 200:
        print(f"❌ 上传失败: {response.status_code}")
        print(response.text)
        return

    upload_result = response.json()
    task_id = upload_result['task_id']
    print(f"✅ 上传成功! Task ID: {task_id}")
    print(f"   文件名: {upload_result['filename']}")
    print(f"   状态: {upload_result['status']}")

    # 步骤2: 启动分析
    print(f"\n[步骤2] 启动文档分析...")
    response = requests.post(f"{BASE_URL}/analyze/{task_id}")

    if response.status_code != 200:
        print(f"❌ 启动分析失败: {response.status_code}")
        print(response.text)
        return

    print("✅ 分析任务已启动")

    # 步骤3: 轮询结果
    print(f"\n[步骤3] 等待分析完成...")
    max_attempts = 60
    attempt = 0

    while attempt < max_attempts:
        time.sleep(5)
        attempt += 1

        response = requests.get(f"{BASE_URL}/result/{task_id}")
        if response.status_code != 200:
            print(f"❌ 获取结果失败: {response.status_code}")
            continue

        result = response.json()
        status = result['status']
        progress = result.get('progress', 0)
        current_agent = result.get('current_agent', '')

        print(f"   尝试 {attempt}/{max_attempts}: 状态={status}, 进度={progress}%, 当前智能体={current_agent}")

        if status == 'completed':
            print("✅ 分析完成!")
            review_result = result['result']

            # 显示统计信息
            print(f"\n📊 审查结果统计:")
            print(f"   总修改建议: {review_result['total_changes']}")
            print(f"   错别字: {review_result['typo_count']}")
            print(f"   语义问题: {review_result['fluency_count']}")
            print(f"   重复内容: {review_result['duplicate_count']}")
            print(f"   合规性问题: {review_result['compliance_count']}")

            # 显示所有修改建议
            print(f"\n📝 修改建议详情:")
            all_changes = review_result['all_changes']
            for i, change in enumerate(all_changes[:10], 1):  # 只显示前10个
                print(f"\n   [{i}] 类型: {change.get('type', 'unknown')}")
                if 'original' in change:
                    print(f"       原文: {change['original'][:50]}...")
                if 'corrected' in change:
                    print(f"       修正: {change['corrected'][:50]}...")
                if 'improved' in change:
                    print(f"       改进: {change['improved'][:50]}...")
                if 'reason' in change:
                    print(f"       原因: {change['reason'][:50]}...")

            if len(all_changes) > 10:
                print(f"\n   ... 还有 {len(all_changes) - 10} 个修改建议")

            # 步骤4: 应用修改
            print(f"\n[步骤4] 应用修改...")

            # 选择所有修改
            change_ids = [change['id'] for change in all_changes]

            apply_response = requests.post(
                f"{BASE_URL}/apply-changes",
                json={
                    "file_id": task_id,
                    "accepted_change_ids": change_ids
                }
            )

            if apply_response.status_code != 200:
                print(f"❌ 应用修改失败: {apply_response.status_code}")
                print(apply_response.text)
                return

            apply_result = apply_response.json()
            print(f"✅ 修改已应用!")
            print(f"   接受的修改数: {apply_result['total_accepted']}")
            print(f"   成功应用: {apply_result['applied_count']}")
            print(f"   应用失败: {apply_result['failed_count']}")

            # 步骤5: 下载修改后的文档
            print(f"\n[步骤5] 下载修改后的文档...")

            download_response = requests.get(f"{BASE_URL}/download/{task_id}")

            if download_response.status_code != 200:
                print(f"❌ 下载失败: {download_response.status_code}")
                print(download_response.text)
                return

            # 保存下载的文件
            output_path = Path("backend/data/uploads/test_review_modified.docx")
            with open(output_path, 'wb') as f:
                f.write(download_response.content)

            print(f"✅ 文档下载成功!")
            print(f"   保存路径: {output_path}")

            # 测试完成
            print("\n" + "=" * 80)
            print("✅ 完整流程测试成功!")
            print("=" * 80)

            return True

        elif status == 'failed':
            print(f"❌ 分析失败: {result.get('error', '未知错误')}")
            return

    print(f"❌ 分析超时 (超过 {max_attempts * 5} 秒)")

if __name__ == "__main__":
    test_complete_workflow()
