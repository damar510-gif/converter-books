import argparse
import sys
import os
import re
import time
import pymupdf
import subprocess
from google import genai
from google.genai import types

def apply_smart_indexing(text):
    """
    Applies smart markdown indexing (TOC) to unformatted Arabic text.
    """
    # Main headings: كتاب، باب، مقدمة، خاتمة
    text = re.sub(r'^\s*(كتاب|باب|مقدمة|المقدمة|خاتمة|الخاتمة)\b(.*?)$', r'# \1\2', text, flags=re.MULTILINE)

    # Subheadings: فصل، مبحث
    text = re.sub(r'^\s*(فصل|الفصل|مبحث|المبحث)\b(.*?)$', r'## \1\2', text, flags=re.MULTILINE)

    return text

def extract_text_with_gemini_batch(images_bytes):
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("Warning: GEMINI_API_KEY environment variable not set. Skipping OCR for scanned pages.")
        return ""

    model = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")

    try:
        client = genai.Client(api_key=api_key)
        prompt = "Extract the Arabic text from these images. Output only the text in sequence, maintaining the original paragraphs and structure. Keep it RTL compatible."

        contents = []
        for img_bytes in images_bytes:
            contents.append(types.Part.from_bytes(data=img_bytes, mime_type='image/png'))
        contents.append(prompt)

        try:
            response = client.models.generate_content(
                model=model,
                contents=contents
            )
            time.sleep(4.5) # Rate limit handling
            return response.text + "\n\n"
        except Exception as api_err:
            if '429' in str(api_err) or 'quota' in str(api_err).lower():
                print(f"Rate limit or quota hit with {model}. Falling back to gemini-3.1-flash-lite...")
                response = client.models.generate_content(
                    model='gemini-3.1-flash-lite',
                    contents=contents
                )
                time.sleep(4.5)
                return response.text + "\n\n"
            else:
                raise api_err

    except Exception as e:
        print(f"Error calling Gemini API: {e}")
        return ""


def extract_cover_from_pdf(filepath, cover_path):
    try:
        doc = pymupdf.open(filepath)
        if len(doc) > 0:
            page = doc[0]
            pix = page.get_pixmap()
            pix.save(cover_path)
            print(f"Saved cover image to {cover_path}")
            return True
    except Exception as e:
        print(f"Warning: Could not extract cover from PDF: {e}")
    return False

def generate_epub_from_pdf(filepath, output_path, cover_path=None):
    doc = pymupdf.open(filepath)
    full_text = ""

    scanned_batch = []

    for page in doc:
        text = page.get_text()

        # If the page has very little text, it might be a scanned page.
        if len(text.strip()) < 50:
            pix = page.get_pixmap()
            scanned_batch.append(pix.tobytes("png"))

            # Process batch if it reaches 10 pages
            if len(scanned_batch) == 10:
                print(f"Processing batch of 10 scanned pages...")
                ocr_text = extract_text_with_gemini_batch(scanned_batch)
                if ocr_text:
                    full_text += ocr_text
                scanned_batch = []
        else:
            # Process any pending scanned batch before appending raw text
            if scanned_batch:
                print(f"Processing batch of {len(scanned_batch)} scanned pages...")
                ocr_text = extract_text_with_gemini_batch(scanned_batch)
                if ocr_text:
                    full_text += ocr_text
                scanned_batch = []

            full_text += text + "\n\n"

    # Process any remaining scanned pages in the batch
    if scanned_batch:
        print(f"Processing final batch of {len(scanned_batch)} scanned pages...")
        ocr_text = extract_text_with_gemini_batch(scanned_batch)
        if ocr_text:
            full_text += ocr_text

    full_text = apply_smart_indexing(full_text)

    temp_md = "temp.md"

    # Write intermediate markdown with RTL configuration
    md_content = f"""---
title: "Generated EPUB"
dir: rtl
lang: ar-SA
---

{full_text}
"""
    with open(temp_md, "w", encoding="utf-8") as f:
        f.write(md_content)

    cmd = ["pandoc", temp_md, "-o", output_path, "--toc", "--toc-depth=2"]
    if cover_path and os.path.exists(cover_path):
        cmd.extend(["--epub-cover-image", cover_path])

    try:
        print(f"Running pandoc to generate {output_path}...")
        subprocess.run(cmd, check=True)
        print(f"Successfully generated EPUB: {output_path}")
    except subprocess.CalledProcessError as e:
        print(f"Error running pandoc: {e}")
        raise
    except FileNotFoundError:
        print("Error: Pandoc is not installed or not in PATH.")
        raise
    finally:
        if os.path.exists(temp_md):
            os.remove(temp_md)

def generate_epub_from_txt(filepath, output_path):
    with open(filepath, 'r', encoding='utf-8') as f:
        text = f.read()

    text = apply_smart_indexing(text)

    temp_md = "temp.md"

    # Write intermediate markdown with RTL configuration
    md_content = f"""---
title: "Generated EPUB"
dir: rtl
lang: ar-SA
---

{text}
"""
    with open(temp_md, "w", encoding="utf-8") as f:
        f.write(md_content)

    cmd = ["pandoc", temp_md, "-o", output_path, "--toc", "--toc-depth=2"]

    try:
        print(f"Running pandoc to generate {output_path}...")
        subprocess.run(cmd, check=True)
        print(f"Successfully generated EPUB: {output_path}")
    except subprocess.CalledProcessError as e:
        print(f"Error running pandoc: {e}")
        raise
    except FileNotFoundError:
        print("Error: Pandoc is not installed or not in PATH.")
        raise
    finally:
        if os.path.exists(temp_md):
            os.remove(temp_md)

def generate_epub_from_docx(filepath, output_path):
    # Pass DOCX directly to Pandoc to preserve chapters and footnotes!
    cmd = [
        "pandoc",
        filepath,
        "-o", output_path,
        "-M", "dir=rtl",
        "-M", "lang=ar-SA"
    ]

    try:
        print(f"Running pandoc to generate {output_path} from DOCX directly...")
        subprocess.run(cmd, check=True)
        print(f"Successfully generated EPUB: {output_path}")
    except subprocess.CalledProcessError as e:
        print(f"Error running pandoc: {e}")
        raise
    except FileNotFoundError:
        print("Error: Pandoc is not installed or not in PATH.")
        raise

def process_file(filepath, output_path):
    ext = os.path.splitext(filepath)[1].lower()

    if ext == '.pdf':
        cover_path = "temp_cover.png"
        if not extract_cover_from_pdf(filepath, cover_path):
            cover_path = None
        try:
            generate_epub_from_pdf(filepath, output_path, cover_path)
        finally:
            if cover_path and os.path.exists(cover_path):
                os.remove(cover_path)
    elif ext == '.docx':
        generate_epub_from_docx(filepath, output_path)
    elif ext == '.txt':
        generate_epub_from_txt(filepath, output_path)
    else:
        raise ValueError(f"Unsupported file type: {ext}")

def main():
    parser = argparse.ArgumentParser(description="Convert PDF, DOCX, TXT to EPUB")
    parser.add_argument("input", help="Path to input file")
    parser.add_argument("-o", "--output", help="Path to output EPUB file (optional)")
    args = parser.parse_args()

    input_path = args.input
    if not os.path.exists(input_path):
        print(f"Error: File not found - {input_path}")
        sys.exit(1)

    output_path = args.output
    if not output_path:
        base_name = os.path.splitext(os.path.basename(input_path))[0]
        output_path = f"{base_name}.epub"

    print(f"Processing {input_path}...")
    try:
        process_file(input_path, output_path)
    except Exception as e:
        print(f"Error during conversion pipeline: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
