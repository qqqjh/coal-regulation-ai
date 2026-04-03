"""
测试文档审查完整流程
"""
from docx import Document
from pathlib import Path

# 创建测试Word文档
doc = Document()

# 添加标题
doc.add_heading('煤矿安全生产管理制度', 0)

# 添加段落(包含一些故意的错误)
doc.add_paragraph('第一章 总则')
doc.add_paragraph('第一条 为了加强煤矿安全生产管理，保障职工生命安全，根剧《煤矿安全规程》制定本制度。')  # 错别字: 根剧->根据

doc.add_paragraph('第二条 煤矿企业必须坚持"安全第一、预防为主、综和治理"的方针。')  # 错别字: 综和->综合

doc.add_paragraph('第二章 瓦斯管理')
doc.add_paragraph('第三条 矿井必须建立瓦斯检测制度。瓦斯检测应当每4小时进行一次检测。')  # 合规性问题: 应该是每2小时

doc.add_paragraph('第四条 采掘工作面的瓦斯浓度不得超过1.5%，当瓦斯浓度达到1.5%时必须停止作业。')  # 合规性问题: 应该是1.0%

doc.add_paragraph('第三章 通风系统')
doc.add_paragraph('第五条 矿井必须建立完善的通风系统，主要通风机应当安装在井下。')  # 合规性问题: 应该安装在地面

doc.add_paragraph('第六条 通风系统应当定期检查维护，防爆门每年检查维修1次。')  # 合规性问题: 应该是每6个月

doc.add_paragraph('第四章 安全责任')
doc.add_paragraph('第七条 企业必须建立健全安全生产责任制。企业必须建立健全安全生产责任制。')  # 重复内容

doc.add_paragraph('第八条 矿长是安全生产的第一责任人，对安全生产工作全面负责，必须确保安全。')  # 语义问题: 句子冗长

# 保存文档
output_path = Path('backend/data/uploads/test_review.docx')
output_path.parent.mkdir(parents=True, exist_ok=True)
doc.save(output_path)

print(f"测试文档已创建: {output_path}")
print("\n文档包含以下问题:")
print("1. 错别字: '根剧' -> '根据', '综和' -> '综合'")
print("2. 合规性问题: 瓦斯检测频率、瓦斯浓度限值、通风机位置、防爆门检查频率")
print("3. 重复内容: '企业必须建立健全安全生产责任制' 重复")
print("4. 语义问题: 第八条句子冗长")
