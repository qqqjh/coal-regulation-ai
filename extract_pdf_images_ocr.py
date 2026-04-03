import fitz
from PIL import Image
import pytesseract
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
import io
import os

pdf_path = r"D:\work\project\coal-regulation-ai\new_docs\rule_docs\山西省煤矿顶板安全管理规定.pdf"
output_txt = "extracted_山西省煤矿顶板安全管理规定.txt"
output_pdf = "extracted_山西省煤矿顶板安全管理规定.pdf"
pytesseract.pytesseract.tesseract_cmd = r'D:\Tesseract\tesseract.exe'
doc = fitz.open(pdf_path)
all_text = []

for page_num in range(len(doc)):
    page = doc[page_num]
    images = page.get_images()

    for img_index, img in enumerate(images):
        xref = img[0]
        base_image = doc.extract_image(xref)
        image_bytes = base_image["image"]

        image = Image.open(io.BytesIO(image_bytes))
        text = pytesseract.image_to_string(image, lang='chi_sim')
        text = text.replace(' ', '')
        all_text.append(f"Page {page_num + 1}, Image {img_index + 1}:\n{text}\n")

doc.close()

with open(output_txt, 'w', encoding='utf-8') as f:
    f.write('\n'.join(all_text))

pdfmetrics.registerFont(TTFont('SimSun', 'C:\\Windows\\Fonts\\simsun.ttc'))
c = canvas.Canvas(output_pdf, pagesize=A4)
c.setFont('SimSun', 10)

y = 800
for line in '\n'.join(all_text).split('\n'):
    if y < 50:
        c.showPage()
        c.setFont('SimSun', 10)
        y = 800
    c.drawString(50, y, line[:80])
    y -= 15

c.save()
print(f"提取完成: {output_txt}, {output_pdf}")
