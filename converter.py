import argparse
import sys
import os
import pymupdf
import subprocess
from google import genai
from google.genai import types

def extract_text_with_gemini(image_bytes):
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("Warning: GEMINI_API_KEY environment variable not set. Skipping OCR for scanned page.")
        return ""

    try:
        client = genai.Client(api_key=api_key)

        # We use a system instruction to ensure it outputs raw Arabic text properly.
        prompt = "Extract the Arabic text from this image. Output only the text, maintaining the original paragraphs and structure. Keep it RTL compatible."

        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=[
                types.Part.from_bytes(data=image_bytes, mime_type='image/png'),
                prompt
            ]
        )
        return response.text + "\n\n"
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
    for page in doc:
        text = page.get_text()

        # If the page has very little text, it might be a scanned page.
        if len(text.strip()) < 50:
            print(f"Page {page.number} seems to be scanned or empty. Attempting OCR...")
            # Convert page to image
            pix = page.get_pixmap()
            image_bytes = pix.tobytes("png")

            ocr_text = extract_text_with_gemini(image_bytes)
            if ocr_text:
                full_text += ocr_text
            else:
                full_text += text + "\n\n"
        else:
            full_text += text + "\n\n"

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

    cmd = ["pandoc", temp_md, "-o", output_path]
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

    cmd = ["pandoc", temp_md, "-o", output_path]

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
