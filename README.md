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
The script detects pages without a text layer and sends each one to Gemini on its own, at the scan's native resolution:

- **One page per request**, paced to stay under the per-minute limit (`--rpm`, default 12).
- **Retries** on busy/temporary errors; pages that come back empty or too short are retried once with the stronger `--fallback-model` (limited by `--fallback-limit`).
- **Progress is saved per page** in `--work-dir` (`work/`). Running again only processes missing pages, so a run stopped by the daily limit continues the next day.
- A **report** (`report.md`) lists any missing pages, and the script exits with code 2 when the book is incomplete.
- Headings, footnotes and poetry are kept: each article becomes a chapter in the table of contents.
- `--pages 1-20` converts part of a book (useful for a quick test).
- **Missing-lines review**: when Tesseract is installed, each page is compared with a Tesseract reading; pages whose text lost lines are re-read once (`--review-limit`, default 60 pages).
- The table of contents lists the main articles only (one short text request picks them), and the EPUB carries the original page numbers (a page list), so readers can show the book's own 246 pages.

## GitHub Actions Usage

This repository includes a GitHub Actions workflow that allows you to run the conversion pipeline in the cloud and download the resulting EPUB as a workflow artifact.

1. Navigate to the **Actions** tab in this repository.
2. Select the **Convert Book to EPUB** workflow.
3. Click **Run workflow**.
4. In the inputs, provide the filename of the book you want to convert (assuming you have committed the book file to the repository).
5. If you are processing scanned PDFs, make sure to add a repository secret named `GEMINI_API_KEY` in `Settings -> Secrets and variables -> Actions`.
6. Once the workflow completes successfully, download the generated `.epub` file from the **Artifacts** section at the bottom of the run summary.
