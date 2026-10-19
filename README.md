# Arabic Book Converter (PDF/DOCX/TXT to EPUB)

A robust, fully integrated pipeline to convert books from various formats (PDF, scanned PDF, DOCX, TXT) into professional EPUB files. It has native support for Arabic (RTL) text and includes smart OCR using Google Gemini for scanned documents.

## Features
- **Format Support**: Natively extracts text from PDF, DOCX, and TXT files using `PyMuPDF`.
- **RTL & Arabic Support**: Automatically configures the output EPUB for Right-to-Left languages.
- **Smart OCR for Scanned PDFs**: Automatically detects scanned pages in PDFs and uses the Google Gemini API to extract Arabic text perfectly, without losing structure.
- **Cover Image Generation**: Extracts the first page of the input PDF to serve as the generated EPUB's cover.
- **GitHub Actions Integration**: Designed to be run headlessly in CI/CD environments.

## Local Installation

1. **Install Pandoc:**
   The conversion process requires Pandoc to be installed on your system.
   - **Ubuntu/Debian**: `sudo apt-get install pandoc`
   - **macOS**: `brew install pandoc`
   - **Windows**: Download from the [Pandoc releases page](https://github.com/jgm/pandoc/releases).

2. **Install Python Dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

## Local Usage

Run the converter script against any supported file type:

```bash
# Convert a basic text file
python converter.py mybook.txt

# Convert a PDF (will output mybook.epub)
python converter.py mybook.pdf

# Specify an output path
python converter.py mybook.docx -o output/my_custom_book.epub
```

### Using Gemini OCR (For Scanned PDFs)
To process scanned PDFs where pages contain images instead of text, you must provide a Gemini API key:

```bash
export GEMINI_API_KEY="your_api_key_here"
python converter.py scanned_book.pdf
```
The script detects pages without a text layer and reads each one on its own, at the scan's native resolution:

- **Two independent readings**: every page is read by `--model` (default `gemini-3.5-flash-lite`) and by `--second-model` (default `gemini-3.1-flash-lite`). Each model has its own free daily quota.
- **Automatic check**: the two readings are compared word by word (spelling variants such as ى/ي are ignored). Pages where they differ, or where Tesseract sees lines missing, get one more request: the image plus both readings, asking for the final text. The answer is accepted only if it is close to the readings and loses no lines; otherwise the better reading is kept and the page is listed as unsure in the report.
- **Refused pages** (Gemini's safety or copyright filters) are read again in two halves, and then by `--rescue-model` (default `gemma-4-26b-a4b-it`).
- **Progress is saved per page** in `--work-dir`. When a model's daily quota runs out the run stops (exit code 3, "paused") and the next run continues where it stopped.
- `report.md` and `status.json` describe the result. Exit codes: 0 done, 2 some pages unreadable, 3 paused for the daily quota.
- Headings, footnotes and poetry are kept; the table of contents lists the main articles only, and the EPUB carries the original page numbers.
- `--pages 1-20` converts part of a book; `--single` reads each page once.

Models can also be set with the repository variables `GEMINI_MODEL`, `GEMINI_SECOND_MODEL` and `GEMINI_RESCUE_MODEL`.

## Drive inbox (no GitHub needed day to day)

Books dropped into a Google Drive folder are converted automatically; see `apps-script/SETUP-ar.md` (Arabic setup guide) and `docs/drive-inbox.md` (how it works).

## GitHub Actions Usage

This repository includes a GitHub Actions workflow that allows you to run the conversion pipeline in the cloud and download the resulting EPUB as a workflow artifact.

1. Navigate to the **Actions** tab in this repository.
2. Select the **Convert Book to EPUB** workflow.
3. Click **Run workflow**.
4. In the inputs, provide the filename of the book you want to convert (assuming you have committed the book file to the repository).
5. If you are processing scanned PDFs, make sure to add a repository secret named `GEMINI_API_KEY` in `Settings -> Secrets and variables -> Actions`.
6. Once the workflow completes successfully, download the generated `.epub` file from the **Artifacts** section at the bottom of the run summary.
