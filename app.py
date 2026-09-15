"""Flask application for CiteAI."""

import os
import hmac
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import uuid4

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, send_file, session
from werkzeug.utils import secure_filename

load_dotenv()

from database import delete_document, get_supabase, list_documents
from ingest import ingest_pdf
from rag import answer_question


app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "change-this-development-secret")
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024
ALLOWED_EXTENSION = ".pdf"
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", "uploads"))
UPLOAD_DIR.mkdir(exist_ok=True)


def current_principal() -> dict | None:
    return session.get("principal")


def principal_or_error() -> tuple[dict | None, tuple]:
    principal = current_principal()
    if not principal:
        return None, (jsonify({"error": "Authentication required."}), 401)
    return principal, ()


def _auth_payload() -> dict:
    return request.get_json(silent=True) or {}


def available_documents(principal: dict) -> list[dict]:
    """Only expose indexed documents whose authenticated PDF is available."""
    documents = list_documents(principal["id"], principal["role"] == "admin")
    return [
        document
        for document in documents
        if (UPLOAD_DIR / f"{document['document_id']}.pdf").is_file()
    ]


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/auth/me")
def auth_me():
    return jsonify({"user": current_principal()})


@app.post("/auth/signup")
def signup():
    payload = _auth_payload()
    email = str(payload.get("email", "")).strip().lower()
    password = str(payload.get("password", ""))
    if "@" not in email or len(password) < 8:
        return jsonify({"error": "Use a valid email and a password of at least 8 characters."}), 400
    try:
        response = get_supabase().auth.sign_up({"email": email, "password": password})
        if not response.user:
            return jsonify({"error": "Sign-up did not create a user."}), 400
        if response.session:
            session["principal"] = {
                "id": str(response.user.id),
                "email": response.user.email,
                "role": "user",
            }
            return jsonify({"user": session["principal"]}), 201
        return jsonify({"message": "Check your email to confirm your account."}), 201
    except Exception:
        app.logger.exception("Sign-up failed")
        return jsonify({"error": "Unable to create the account."}), 400


@app.post("/auth/login")
def login():
    payload = _auth_payload()
    email = str(payload.get("email", "")).strip().lower()
    password = str(payload.get("password", ""))
    try:
        response = get_supabase().auth.sign_in_with_password(
            {"email": email, "password": password}
        )
        if not response.user:
            raise ValueError("Invalid login")
        session["principal"] = {
            "id": str(response.user.id),
            "email": response.user.email,
            "role": "user",
        }
        return jsonify({"user": session["principal"]})
    except Exception:
        app.logger.exception("User login failed")
        return jsonify({"error": "Invalid email or password."}), 401


@app.post("/auth/admin-login")
def admin_login():
    payload = _auth_payload()
    username = str(payload.get("username", ""))
    password = str(payload.get("password", ""))
    expected_username = os.getenv("ADMIN_USERNAME", "admin")
    expected_password = os.getenv("ADMIN_PASSWORD")
    if expected_password and hmac.compare_digest(username, expected_username) and hmac.compare_digest(password, expected_password):
        session["principal"] = {"id": "admin", "email": username, "role": "admin"}
        return jsonify({"user": session["principal"]})
    return jsonify({"error": "Invalid admin credentials."}), 401


@app.post("/auth/logout")
def logout():
    session.clear()
    return jsonify({"message": "Logged out."})


@app.get("/documents")
def documents():
    principal, error = principal_or_error()
    if error:
        return error
    try:
        return jsonify({
            "documents": available_documents(principal)
        })
    except Exception as error:
        app.logger.exception("Unable to list documents")
        return jsonify({"error": str(error)}), 500


@app.post("/upload")
def upload():
    principal, error = principal_or_error()
    if error:
        return error
    uploaded_file = request.files.get("file")
    if not uploaded_file or not uploaded_file.filename:
        return jsonify({"error": "Please choose a PDF file."}), 400

    document_name = secure_filename(uploaded_file.filename)
    if Path(document_name).suffix.lower() != ALLOWED_EXTENSION:
        return jsonify({"error": "Only PDF files are supported."}), 400

    temporary_path = None
    document_id = str(uuid4())
    stored_path = UPLOAD_DIR / f"{document_id}.pdf"
    try:
        with NamedTemporaryFile(suffix=ALLOWED_EXTENSION, delete=False) as temporary_file:
            temporary_path = temporary_file.name
        uploaded_file.save(temporary_path)
        _, chunk_count = ingest_pdf(
            temporary_path,
            document_name,
            principal["id"],
            document_id,
        )
        os.replace(temporary_path, stored_path)
        temporary_path = None
        return jsonify({
            "document_id": document_id,
            "document_name": document_name,
            "chunks": chunk_count,
        }), 201
    except Exception as error:
        app.logger.exception("PDF indexing failed")
        return jsonify({"error": str(error)}), 500
    finally:
        if temporary_path:
            try:
                os.remove(temporary_path)
            except OSError:
                app.logger.warning("Could not remove temporary file %s", temporary_path)


@app.delete("/documents/<document_id>")
def remove_document(document_id: str):
    principal, error = principal_or_error()
    if error:
        return error
    try:
        deleted_chunks = delete_document(
            document_id,
            principal["id"],
            principal["role"] == "admin",
        )
        if not deleted_chunks:
            return jsonify({"error": "Document not found or not accessible."}), 404
        (UPLOAD_DIR / f"{document_id}.pdf").unlink(missing_ok=True)
        return jsonify({"deleted_chunks": deleted_chunks})
    except Exception:
        app.logger.exception("Document deletion failed")
        return jsonify({"error": "Unable to delete the document."}), 500


@app.get("/documents/<document_id>/file")
def document_file(document_id: str):
    principal, error = principal_or_error()
    if error:
        return error
    try:
        visible_ids = {
            document["document_id"] for document in available_documents(principal)
        }
        if document_id not in visible_ids:
            return jsonify({"error": "Document not found or not accessible."}), 404
        stored_path = UPLOAD_DIR / f"{document_id}.pdf"
        if not stored_path.is_file():
            return jsonify({"error": "PDF file is not available."}), 404
        return send_file(stored_path, mimetype="application/pdf", as_attachment=False)
    except Exception:
        app.logger.exception("PDF delivery failed")
        return jsonify({"error": "Unable to load the PDF."}), 500


@app.post("/ask")
def ask():
    principal, error = principal_or_error()
    if error:
        return error
    payload = request.get_json(silent=True) or {}
    question = str(payload.get("question", "")).strip()
    if not question:
        return jsonify({"error": "Please enter a question."}), 400

    try:
        document_ids = payload.get("document_ids") or []
        if not isinstance(document_ids, list) or not all(isinstance(item, str) for item in document_ids):
            return jsonify({"error": "document_ids must be a list of strings."}), 400
        visible_ids = {
            document["document_id"] for document in available_documents(principal)
        }
        document_ids = [document_id for document_id in document_ids if document_id in visible_ids]
        if not payload.get("document_ids"):
            document_ids = sorted(visible_ids)
        history = payload.get("history") or []
        if not isinstance(history, list):
            return jsonify({"error": "history must be a list."}), 400
        return jsonify(answer_question(
            question,
            principal["id"],
            document_ids,
            principal["role"] == "admin",
            history,
        ))
    except Exception as error:
        app.logger.exception("Question answering failed")
        return jsonify({"error": str(error)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=True)