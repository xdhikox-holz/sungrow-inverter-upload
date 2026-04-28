"""
Sungrow Inverter Inspection Upload System
Web app untuk upload dokumentasi pengecekan inverter ke Google Drive
"""

import streamlit as st
import re
from datetime import datetime
from PIL import Image
from PIL.ExifTags import TAGS
import io
import json
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# ============================================
# KONFIGURASI
# ============================================
KEGIATAN_FOLDERS = {
    'Update Firmware': '01_Update_Firmware',
    'DC SPD': '02_DC_SPD',
    'Thermal Imaging': '03_Thermal_Imaging',
    'Fault Recorder': '04_Fault_Recorder'
}

ROOT_FOLDER_ID = st.secrets.get("ROOT_FOLDER_ID", "")
ADMIN_EMAIL = st.secrets.get("ADMIN_EMAIL", "")
GMAIL_USER = st.secrets.get("GMAIL_USER", "")
GMAIL_APP_PASSWORD = st.secrets.get("GMAIL_APP_PASSWORD", "")

# ============================================
# GOOGLE DRIVE SETUP
# ============================================
@st.cache_resource
def get_drive_service():
    """Initialize Google Drive API service"""
    credentials_dict = json.loads(st.secrets["gcp_service_account"])
    credentials = service_account.Credentials.from_service_account_info(
        credentials_dict,
        scopes=['https://www.googleapis.com/auth/drive']
    )
    service = build('drive', 'v3', credentials=credentials)
    return service

# ============================================
# HELPER FUNCTIONS
# ============================================
def validate_sn(sn):
    """Validate SN: harus 11 karakter, kombinasi huruf + angka"""
    if len(sn) != 11:
        return False, "Serial Number harus tepat 11 karakter"
    if not re.match(r'^[A-Za-z0-9]{11}$', sn):
        return False, "Serial Number hanya boleh huruf dan angka (tanpa spasi/simbol)"
    return True, ""

def get_exif_timestamp(image_bytes, filename):
    """Extract timestamp dari EXIF, fallback ke nama file, fallback ke now()"""
    try:
        img = Image.open(io.BytesIO(image_bytes))
        exif_data = img._getexif()
        if exif_data:
            for tag_id, value in exif_data.items():
                tag = TAGS.get(tag_id, tag_id)
                if tag == 'DateTimeOriginal':
                    return datetime.strptime(value, '%Y:%m:%d %H:%M:%S')
    except Exception:
        pass
    
    # Fallback 1: parse dari nama file (IMG_20260428_091532.jpg)
    match = re.search(r'(\d{4})(\d{2})(\d{2})[_\-]?(\d{2})(\d{2})(\d{2})', filename)
    if match:
        try:
            return datetime(*[int(x) for x in match.groups()])
        except Exception:
            pass
    
    # Fallback 2: timestamp upload
    return datetime.now()

def find_folder(service, name, parent_id):
    """Cari folder by name di parent, return folder ID atau None"""
    query = f"name='{name}' and '{parent_id}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false"
    results = service.files().list(q=query, fields="files(id, name)").execute()
    files = results.get('files', [])
    return files[0]['id'] if files else None

def create_folder(service, name, parent_id):
    """Buat folder baru di parent"""
    file_metadata = {
        'name': name,
        'mimeType': 'application/vnd.google-apps.folder',
        'parents': [parent_id]
    }
    folder = service.files().create(body=file_metadata, fields='id').execute()
    return folder.get('id')

def count_files_in_folder(service, folder_id):
    """Hitung jumlah file di folder (untuk continuing sequence number)"""
    query = f"'{folder_id}' in parents and trashed=false and mimeType!='application/vnd.google-apps.folder'"
    results = service.files().list(q=query, fields="files(id)").execute()
    return len(results.get('files', []))

def upload_file(service, file_bytes, filename, mime_type, folder_id):
    """Upload file ke folder"""
    file_metadata = {'name': filename, 'parents': [folder_id]}
    media = MediaIoBaseUpload(io.BytesIO(file_bytes), mimetype=mime_type, resumable=True)
    file = service.files().create(body=file_metadata, media_body=media, fields='id, webViewLink').execute()
    return file.get('id'), file.get('webViewLink')

def get_or_create_log(service, sn_folder_id):
    """Cari log.txt atau return None kalau belum ada"""
    query = f"name='log.txt' and '{sn_folder_id}' in parents and trashed=false"
    results = service.files().list(q=query, fields="files(id)").execute()
    files = results.get('files', [])
    return files[0]['id'] if files else None

def update_log(service, sn_folder_id, kegiatan, count, first_time, last_time, catatan):
    """Update atau buat log.txt"""
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    first_str = first_time.strftime('%H:%M:%S') if first_time else '-'
    last_str = last_time.strftime('%H:%M:%S') if last_time else '-'
    note_str = f' | Note: {catatan}' if catatan else ''
    new_entry = f"[{timestamp}] Kegiatan: {kegiatan} | {count} file ditambahkan | Range: {first_str} - {last_str}{note_str}\n"
    
    log_id = get_or_create_log(service, sn_folder_id)
    
    if log_id:
        # Append to existing log
        existing = service.files().get_media(fileId=log_id).execute().decode('utf-8')
        updated_content = existing + new_entry
        media = MediaIoBaseUpload(io.BytesIO(updated_content.encode('utf-8')), mimetype='text/plain')
        service.files().update(fileId=log_id, media_body=media).execute()
    else:
        # Create new log
        media = MediaIoBaseUpload(io.BytesIO(new_entry.encode('utf-8')), mimetype='text/plain')
        file_metadata = {'name': 'log.txt', 'parents': [sn_folder_id]}
        service.files().create(body=file_metadata, media_body=media, fields='id').execute()

def send_notification_email(sn, kegiatan, count, status, folder_url, first_time, last_time, catatan):
    """Kirim email notifikasi ke admin"""
    if not GMAIL_USER or not GMAIL_APP_PASSWORD or not ADMIN_EMAIL:
        return False, "Email config not set"
    
    subject = f"[Sungrow] {status} - SN {sn}"
    
    first_str = first_time.strftime('%Y-%m-%d %H:%M:%S') if first_time else '-'
    last_str = last_time.strftime('%H:%M:%S') if last_time else '-'
    
    body = f"""
{status}

DETAIL SUBMISSION:
Serial Number  : {sn}
Kegiatan       : {kegiatan}
File berhasil  : {count} foto
Range waktu    : {first_str} s/d {last_str}
Catatan        : {catatan or '-'}

Buka folder:
{folder_url}

Submitted: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
"""
    
    try:
        msg = MIMEMultipart()
        msg['From'] = GMAIL_USER
        msg['To'] = ADMIN_EMAIL
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'plain'))
        
        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        server.send_message(msg)
        server.quit()
        return True, "Email sent"
    except Exception as e:
        return False, str(e)

def check_sn_exists(service, sn):
    """Cek apakah folder SN sudah ada di root"""
    return find_folder(service, sn, ROOT_FOLDER_ID) is not None

# ============================================
# STREAMLIT UI
# ============================================
st.set_page_config(
    page_title="Sungrow Inverter Upload",
    page_icon="🔧",
    layout="centered"
)

st.title("🔧 Sungrow Inverter Inspection Upload")
st.caption("Upload dokumentasi pengecekan inverter — auto-organize ke Google Drive")
st.divider()

# Validation: cek apakah secrets sudah di-set
if not ROOT_FOLDER_ID:
    st.error("⚠️ ROOT_FOLDER_ID belum di-set di Streamlit secrets. Hubungi admin.")
    st.stop()

try:
    service = get_drive_service()
except Exception as e:
    st.error(f"⚠️ Gagal connect ke Google Drive: {e}")
    st.stop()

# Form input
sn_input = st.text_input(
    "Serial Number (11 karakter)",
    max_chars=11,
    placeholder="Contoh: A2304567890",
    help="Kombinasi huruf dan angka, tepat 11 karakter"
).strip().upper()

# Real-time validation + check existing
if sn_input:
    valid, msg = validate_sn(sn_input)
    if not valid:
        st.warning(f"⚠️ {msg}")
    else:
        with st.spinner("Cek SN di database..."):
            sn_exists = check_sn_exists(service, sn_input)
        if sn_exists:
            st.warning(f"⚠️ **SN `{sn_input}` sudah terdaftar.** File akan ditambahkan ke folder yang sudah ada.")
        else:
            st.success(f"✅ SN `{sn_input}` baru — folder akan dibuat otomatis.")

kegiatan = st.radio(
    "Pilih Jenis Kegiatan",
    options=list(KEGIATAN_FOLDERS.keys()),
    horizontal=False
)

uploaded_files = st.file_uploader(
    "Upload Foto Dokumentasi (bisa multiple)",
    type=['jpg', 'jpeg', 'png', 'webp', 'heic'],
    accept_multiple_files=True,
    help="Foto akan di-sortir otomatis berdasarkan timestamp EXIF"
)

if uploaded_files:
    st.info(f"📸 {len(uploaded_files)} file dipilih")

catatan = st.text_area(
    "Catatan Tambahan (opsional)",
    placeholder="Misal: nama site, kondisi inverter, dll",
    height=80
)

# Submit
st.divider()
submit = st.button("🚀 Submit Upload", type="primary", use_container_width=True)

if submit:
    # Validation
    if not sn_input:
        st.error("❌ Serial Number wajib diisi")
        st.stop()
    
    valid, msg = validate_sn(sn_input)
    if not valid:
        st.error(f"❌ {msg}")
        st.stop()
    
    if not uploaded_files:
        st.error("❌ Minimal upload 1 foto")
        st.stop()
    
    # Process
    progress = st.progress(0, text="Memulai...")
    
    try:
        # Step 1: Cari/buat folder SN
        progress.progress(10, text="Cek folder SN...")
        sn_folder_id = find_folder(service, sn_input, ROOT_FOLDER_ID)
        is_new_folder = False
        
        if not sn_folder_id:
            progress.progress(20, text="Buat folder baru + 4 subfolder kegiatan...")
            sn_folder_id = create_folder(service, sn_input, ROOT_FOLDER_ID)
            for folder_name in KEGIATAN_FOLDERS.values():
                create_folder(service, folder_name, sn_folder_id)
            is_new_folder = True
        
        # Step 2: Cari subfolder kegiatan (auto-create kalau ga ada)
        target_folder_name = KEGIATAN_FOLDERS[kegiatan]
        target_folder_id = find_folder(service, target_folder_name, sn_folder_id)
        if not target_folder_id:
            target_folder_id = create_folder(service, target_folder_name, sn_folder_id)
        
        # Step 3: Read files + extract timestamp
        progress.progress(30, text="Baca metadata foto...")
        files_data = []
        for f in uploaded_files:
            file_bytes = f.read()
            timestamp = get_exif_timestamp(file_bytes, f.name)
            files_data.append({
                'bytes': file_bytes,
                'original_name': f.name,
                'mime_type': f.type or 'image/jpeg',
                'timestamp': timestamp
            })
        
        # Step 4: Sortir by timestamp (paling awal -> akhir)
        files_data.sort(key=lambda x: x['timestamp'])
        
        # Step 5: Hitung existing files (continuing sequence)
        existing_count = count_files_in_folder(service, target_folder_id)
        
        # Step 6: Upload + rename
        first_ts = None
        last_ts = None
        uploaded_count = 0
        
        for idx, file_data in enumerate(files_data):
            seq = str(existing_count + idx + 1).zfill(3)
            ts = file_data['timestamp']
            date_str = ts.strftime('%Y-%m-%d')
            time_str = ts.strftime('%H-%M-%S')
            ext = file_data['original_name'].split('.')[-1].lower()
            new_name = f"{seq}_{date_str}_{time_str}.{ext}"
            
            progress.progress(
                30 + int((idx + 1) / len(files_data) * 60),
                text=f"Upload {idx + 1}/{len(files_data)}: {new_name}"
            )
            
            upload_file(service, file_data['bytes'], new_name, file_data['mime_type'], target_folder_id)
            
            if first_ts is None:
                first_ts = ts
            last_ts = ts
            uploaded_count += 1
        
        # Step 7: Update log
        progress.progress(95, text="Update log...")
        update_log(service, sn_folder_id, kegiatan, uploaded_count, first_ts, last_ts, catatan)
        
        # Step 8: Send email
        folder_url = f"https://drive.google.com/drive/folders/{sn_folder_id}"
        status = "✅ FOLDER BARU DIBUAT" if is_new_folder else "⚠️ SN SUDAH ADA - File di-append"
        
        email_sent, email_msg = send_notification_email(
            sn_input, kegiatan, uploaded_count, status, folder_url, first_ts, last_ts, catatan
        )
        
        progress.progress(100, text="Selesai!")
        
        # Success message
        st.success("✅ Upload berhasil!")
        
        col1, col2 = st.columns(2)
        with col1:
            st.metric("File Berhasil", uploaded_count)
        with col2:
            st.metric("Status Folder", "Baru" if is_new_folder else "Existing")
        
        st.markdown(f"**📁 [Buka Folder Google Drive]({folder_url})**")
        
        if email_sent:
            st.info(f"📧 Notifikasi email terkirim ke admin")
        else:
            st.warning(f"⚠️ Email notifikasi gagal: {email_msg}")
        
        with st.expander("📋 Detail Submission"):
            st.write(f"**Serial Number:** `{sn_input}`")
            st.write(f"**Kegiatan:** {kegiatan}")
            st.write(f"**Jumlah file:** {uploaded_count}")
            st.write(f"**Range waktu:** {first_ts.strftime('%Y-%m-%d %H:%M:%S')} → {last_ts.strftime('%H:%M:%S')}")
            if catatan:
                st.write(f"**Catatan:** {catatan}")
        
    except Exception as e:
        progress.empty()
        st.error(f"❌ Error: {str(e)}")
        st.exception(e)

# Footer
st.divider()
st.caption("Sungrow Power Supply Co. Ltd. — Technical Service")
