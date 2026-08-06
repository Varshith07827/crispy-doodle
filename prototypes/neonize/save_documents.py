"""Save incoming DOCUMENTS (pdf, docx, xlsx, zip, ...).

Another one winSpark cannot reach today: a document shows as a filename chip in
the chat, and there are no pixels of the file itself to capture. The original
filename and page count are preserved in the sidecar JSON.

    python save_documents.py
"""

import wa_runner

if __name__ == "__main__":
    wa_runner.run({"document"}, label="documents")
