import os
from io import BytesIO
from flask import Blueprint, render_template, request, jsonify, session, send_file
from .db import get_db
from .helpers import now_ist, gen_id, _is_admin, audit_log, UPLOAD_FOLDER, validate_upload
from .decorators import login_required, hr_or_admin_required

documents_bp = Blueprint('documents', __name__)

os.makedirs(UPLOAD_FOLDER, exist_ok=True)


def register_documents(app):
    app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
    app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024
    app.register_blueprint(documents_bp)


@documents_bp.route('/admin/documents')
@hr_or_admin_required
def admin_documents():
    return render_template('admin_documents.html')


@documents_bp.route('/documents')
@login_required
def documents_page():
    return render_template('documents.html')


@documents_bp.route('/api/v1/documents', methods=['GET'])
@documents_bp.route('/api/documents', methods=['GET'])
@login_required
def documents_list():
    conn = get_db()
    context_filter = request.args.get('context', '').strip()
    is_admin = _is_admin(session.get('role'))
    base_sql = "SELECT d.doc_id, d.emp_id, u.name, d.name, d.category, d.file_path, d.file_size, d.uploaded_at, d.context, d.doc_type FROM documents d JOIN users u ON d.emp_id = u.emp_id"
    conditions = []
    params = []
    if not is_admin:
        conditions.append("d.emp_id = ?")
        params.append(session['emp_id'])
    if context_filter and context_filter != 'All':
        conditions.append("d.context = ?")
        params.append(context_filter)
    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    rows = conn.execute(f"{base_sql}{where} ORDER BY d.uploaded_at DESC", params).fetchall()
    conn.close()
    return jsonify([{'id': r[0], 'emp_id': r[1], 'employee': r[2], 'name': r[3], 'category': r[4], 'file_path': r[5], 'file_size': r[6], 'uploaded_at': r[7].isoformat() + '+05:30' if r[7] else None, 'context': r[8], 'doc_type': r[9]} for r in rows]), 200


@documents_bp.route('/api/v1/upload', methods=['POST'])
@documents_bp.route('/api/upload', methods=['POST'])
@login_required
def upload_document():
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400
    f = request.files['file']
    error = validate_upload(f)
    if error:
        return jsonify({'error': error}), 400
    emp_id = request.form.get('emp_id', session['emp_id'])
    category = request.form.get('category', 'Other')
    filename = f"{int(now_ist().timestamp())}_{f.filename}"
    filepath = os.path.join(UPLOAD_FOLDER, filename)
    f.save(filepath)
    fsize = os.path.getsize(filepath)
    context = request.form.get('context', 'General')
    did = gen_id()
    conn = get_db()
    conn.execute(
        "INSERT INTO documents (doc_id, emp_id, name, category, file_path, file_size, uploaded_at, context) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [did, emp_id, f.filename, category, filename, fsize, now_ist(), context]
    )
    conn.close()
    audit_log(session['emp_id'], 'DOCUMENT_UPLOAD', f'Document "{f.filename}" ({category}) uploaded for {emp_id}')
    return jsonify({'message': 'File uploaded', 'id': did, 'path': filename}), 201


@documents_bp.route('/api/v1/documents/<int:did>/download')
@documents_bp.route('/api/documents/<int:did>/download')
@login_required
def download_document(did):
    conn = get_db()
    row = conn.execute("SELECT file_path, name, emp_id FROM documents WHERE doc_id = ?", [did]).fetchone()
    conn.close()
    if not row:
        return jsonify({'error': 'Not found'}), 404
    if row[2] != session['emp_id'] and not _is_admin(session.get('role')):
        return jsonify({'error': 'Forbidden'}), 403
    filepath = os.path.join(UPLOAD_FOLDER, row[0])
    if not os.path.exists(filepath):
        return jsonify({'error': 'File not found on disk'}), 404
    return send_file(filepath, as_attachment=True, download_name=row[1])


@documents_bp.route('/api/v1/documents/<int:did>', methods=['DELETE'])
@documents_bp.route('/api/documents/<int:did>', methods=['DELETE'])
@login_required
def delete_document(did):
    conn = get_db()
    row = conn.execute("SELECT file_path FROM documents WHERE doc_id = ?", [did]).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'Not found'}), 404
    conn.execute("DELETE FROM documents WHERE doc_id = ?", [did])
    conn.close()
    filepath = os.path.join(UPLOAD_FOLDER, row[0])
    if os.path.exists(filepath):
        os.remove(filepath)
    audit_log(session['emp_id'], 'DOCUMENT_DELETE', f'Document {did} deleted')
    return jsonify({'message': 'Document deleted'}), 200
