"""
Sungrow Inverter Inspection Upload System
v5: + auto-clear uploader saat ganti SN/kegiatan
"""

import streamlit as st
import re
import gc
import json
from datetime import datetime
from PIL import Image
from PIL.ExifTags import TAGS
import io
from google.oauth2.credentials import Credentials
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
    'Fault Recorder': '04_Fault_Recorder',
    'DC Insulation Test': '05_DC_Insulation_Test',
    'Inverter Condition': '06_Inverter_Condition'
}

# Kegiatan yang butuh archive upload (bukan foto)
ARCHIVE_KEGIATAN = {'Fault Recorder'}

# File extensions per mode
IMAGE_EXTENSIONS = ['jpg', 'jpeg', 'png', 'webp', 'heic']
ARCHIVE_EXTENSIONS = ['rar', 'zip', 'tar', 'gz', '7z', 'log', 'txt', 'csv']

METADATA_FILENAME = 'inverter_metadata.json'

ROOT_FOLDER_ID = st.secrets.get("ROOT_FOLDER_ID", "")
ADMIN_EMAIL = st.secrets.get("ADMIN_EMAIL", "")
GMAIL_USER = st.secrets.get("GMAIL_USER", "")
GMAIL_APP_PASSWORD = st.secrets.get("GMAIL_APP_PASSWORD", "")

# ============================================
# GOOGLE DRIVE SETUP
# ============================================
@st.cache_resource
def get_drive_service():
    oauth_secrets = st.secrets["oauth_user"]
    creds = Credentials(
        token=None,
        refresh_token=oauth_secrets["refresh_token"],
        token_uri=oauth_secrets["token_uri"],
        client_id=oauth_secrets["client_id"],
        client_secret=oauth_secrets["client_secret"],
        scopes=['https://www.googleapis.com/auth/drive']
    )
    service = build('drive', 'v3', credentials=creds)
    return service

# ============================================
# METADATA HANDLING
# ============================================
def get_metadata_file_id(service):
    query = f"name='{METADATA_FILENAME}' and '{ROOT_FOLDER_ID}' in parents and trashed=false"
    results = service.files().list(q=query, fields="files(id)").execute()
    files = results.get('files', [])
    return files[0]['id'] if files else None

def load_metadata(service):
    file_id = get_metadata_file_id(service)
    if not file_id:
        return {}
    try:
        content = service.files().get_media(fileId=file_id).execute().decode('utf-8')
        return json.loads(content)
    except Exception:
        return {}

def save_metadata(service, metadata):
    content = json.dumps(metadata, indent=2, sort_keys=True)
    media = MediaIoBaseUpload(io.BytesIO(content.encode('utf-8')), mimetype='application/json')
    file_id = get_metadata_file_id(service)
    
    if file_id:
        service.files().update(fileId=file_id, media_body=media).execute()
    else:
        file_metadata = {'name': METADATA_FILENAME, 'parents': [ROOT_FOLDER_ID]}
        service.files().create(body=file_metadata, media_body=media, fields='id').execute()

@st.cache_data(ttl=60)
def get_metadata_cached(_service):
    return load_metadata(_service)

# ============================================
# HELPER FUNCTIONS
# ============================================
def validate_sn(sn):
    if len(sn) != 11:
        return False, "Serial Number harus tepat 11 karakter"
    if not re.match(r'^[A-Za-z0-9]{11}$', sn):
        return False, "Serial Number hanya boleh huruf dan angka (tanpa spasi/simbol)"
    return True, ""

def validate_inv_number(inv):
    if not inv or not inv.strip():
        return False, "Inverter Number wajib diisi"
    if len(inv) > 20:
        return False, "Inverter Number max 20 karakter"
    return True, ""

def get_exif_timestamp_from_uploaded(uploaded_file):
    try:
        pos = uploaded_file.tell()
        uploaded_file.seek(0)
        header_chunk = uploaded_file.read(65536)
        uploaded_file.seek(pos)
        
        img = Image.open(io.BytesIO(header_chunk))
        exif_data = img._getexif()
        if exif_data:
            for tag_id, value in exif_data.items():
                tag = TAGS.get(tag_id, tag_id)
                if tag == 'DateTimeOriginal':
                    return datetime.strptime(value, '%Y:%m:%d %H:%M:%S')
    except Exception:
        pass
    
    match = re.search(r'(\d{4})(\d{2})(\d{2})[_\-]?(\d{2})(\d{2})(\d{2})', uploaded_file.name)
    if match:
        try:
            return datetime(*[int(x) for x in match.groups()])
        except Exception:
            pass
    
    return datetime.now()

def find_folder(service, name, parent_id):
    query = f"name='{name}' and '{parent_id}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false"
    results = service.files().list(q=query, fields="files(id, name)").execute()
    files = results.get('files', [])
    return files[0]['id'] if files else None

def create_folder(service, name, parent_id):
    file_metadata = {
        'name': name,
        'mimeType': 'application/vnd.google-apps.folder',
        'parents': [parent_id]
    }
    folder = service.files().create(body=file_metadata, fields='id').execute()
    return folder.get('id')

def count_files_in_folder(service, folder_id):
    query = f"'{folder_id}' in parents and trashed=false and mimeType!='application/vnd.google-apps.folder'"
    results = service.files().list(q=query, fields="files(id)").execute()
    return len(results.get('files', []))

def upload_file_streaming(service, uploaded_file, filename, folder_id, mime_type=None):
    uploaded_file.seek(0)
    file_metadata = {'name': filename, 'parents': [folder_id]}
    
    if not mime_type:
        ext = filename.split('.')[-1].lower()
        mime_map = {
            'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'png': 'image/png',
            'webp': 'image/webp', 'heic': 'image/heic',
            'rar': 'application/vnd.rar', 'zip': 'application/zip',
            'tar': 'application/x-tar', 'gz': 'application/gzip',
            '7z': 'application/x-7z-compressed',
            'log': 'text/plain', 'txt': 'text/plain', 'csv': 'text/csv'
        }
        mime_type = mime_map.get(ext, 'application/octet-stream')
    
    media = MediaIoBaseUpload(uploaded_file, mimetype=mime_type, resumable=True, chunksize=1024*1024)
    file = service.files().create(body=file_metadata, media_body=media, fields='id').execute()
    return file.get('id')

def get_or_create_log(service, sn_folder_id):
    query = f"name='log.txt' and '{sn_folder_id}' in parents and trashed=false"
    results = service.files().list(q=query, fields="files(id)").execute()
    files = results.get('files', [])
    return files[0]['id'] if files else None

def update_log(service, sn_folder_id, sn, inv_num, kegiatan, count, first_time, last_time, catatan):
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    first_str = first_time.strftime('%H:%M:%S') if first_time else '-'
    last_str = last_time.strftime('%H:%M:%S') if last_time else '-'
    note_str = f' | Note: {catatan}' if catatan else ''
    inv_str = f' | Inverter: {inv_num}' if inv_num else ''
    new_entry = f"[{timestamp}]{inv_str} | Kegiatan: {kegiatan} | {count} file ditambahkan | Range: {first_str} - {last_str}{note_str}\n"
    
    log_id = get_or_create_log(service, sn_folder_id)
    
    if log_id:
        existing = service.files().get_media(fileId=log_id).execute().decode('utf-8')
        updated_content = existing + new_entry
        media = MediaIoBaseUpload(io.BytesIO(updated_content.encode('utf-8')), mimetype='text/plain')
        service.files().update(fileId=log_id, media_body=media).execute()
    else:
        media = MediaIoBaseUpload(io.BytesIO(new_entry.encode('utf-8')), mimetype='text/plain')
        file_metadata = {'name': 'log.txt', 'parents': [sn_folder_id]}
        service.files().create(body=file_metadata, media_body=media, fields='id').execute()

def send_notification_email(sn, inv_num, kegiatan, count, status, folder_url, first_time, last_time, catatan, file_type='foto'):
    if not GMAIL_USER or not GMAIL_APP_PASSWORD or not ADMIN_EMAIL:
        return False, "Email config not set"
    
    inv_label = inv_num if inv_num else '-'
    subject = f"[Sungrow] {status} - SN {sn} ({inv_label})"
    first_str = first_time.strftime('%Y-%m-%d %H:%M:%S') if first_time else '-'
    last_str = last_time.strftime('%H:%M:%S') if last_time else '-'
    
    body = f"""
{status}

DETAIL SUBMISSION:
Serial Number    : {sn}
Inverter Number  : {inv_label}
Kegiatan         : {kegiatan}
File berhasil    : {count} {file_type}
Range waktu      : {first_str} s/d {last_str}
Catatan          : {catatan or '-'}

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

if not ROOT_FOLDER_ID:
    st.error("⚠️ ROOT_FOLDER_ID belum di-set. Hubungi admin.")
    st.stop()

try:
    service = get_drive_service()
except Exception as e:
    st.error(f"⚠️ Gagal connect ke Google Drive: {e}")
    st.stop()

metadata = get_metadata_cached(service)

# ============================================
# MODE SELECTOR
# ============================================
mode = st.radio(
    "Mode Input",
    options=["📋 Pilih dari history", "➕ Daftar SN baru"],
    horizontal=True,
    index=0 if metadata else 1,
)

st.divider()

sn_input = ""
inv_number = ""
is_new_sn = False

# Mode History
if mode == "📋 Pilih dari history":
    if not metadata:
        st.info("📭 Belum ada SN ter-register. Pakai mode **Daftar SN baru** dulu.")
        st.stop()
    
    options = sorted([f"{sn} — {inv}" for sn, inv in metadata.items()])
    selected = st.selectbox(
        f"🔍 Cari Inverter ({len(options)} ter-register)",
        options=["-- Pilih SN --"] + options,
        help="Ketik SN atau Inverter Number untuk filter"
    )
    
    if selected != "-- Pilih SN --":
        sn_input = selected.split(" — ")[0]
        inv_number = selected.split(" — ")[1]
        st.success(f"✅ Dipilih: **{sn_input}** ({inv_number})")
    else:
        st.info("👆 Pilih SN dari dropdown di atas untuk lanjut")
        st.stop()

# Mode New
else:
    sn_input = st.text_input(
        "Serial Number (11 karakter)",
        max_chars=11,
        placeholder="Contoh: A2304567890"
    ).strip().upper()
    
    inv_number = st.text_input(
        "Inverter Number (max 20 karakter)",
        max_chars=20,
        placeholder="Contoh: INV-001"
    ).strip()
    
    if sn_input:
        valid_sn, msg_sn = validate_sn(sn_input)
        if not valid_sn:
            st.warning(f"⚠️ {msg_sn}")
        else:
            if sn_input in metadata:
                existing_inv = metadata[sn_input]
                st.warning(f"⚠️ **SN `{sn_input}` sudah terdaftar** sebagai `{existing_inv}`. Inverter Number akan tetap `{existing_inv}`.")
                inv_number = existing_inv
                is_new_sn = False
            else:
                with st.spinner("Cek SN di Drive..."):
                    sn_exists = check_sn_exists(service, sn_input)
                if sn_exists:
                    st.info(f"ℹ️ SN `{sn_input}` sudah ada folder-nya di Drive (data lama). Inverter Number akan di-link sekarang.")
                    is_new_sn = False
                else:
                    st.success(f"✅ SN `{sn_input}` baru — folder akan dibuat otomatis.")
                    is_new_sn = True
            
            if inv_number and is_new_sn:
                duplicate_sn = [sn for sn, inv in metadata.items() if inv == inv_number and sn != sn_input]
                if duplicate_sn:
                    st.warning(f"⚠️ Inverter Number `{inv_number}` sudah dipakai SN lain: `{duplicate_sn[0]}`.")

st.divider()

# ============================================
# KEGIATAN SELECTOR
# ============================================
kegiatan = st.radio(
    "Pilih Jenis Kegiatan",
    options=list(KEGIATAN_FOLDERS.keys()),
    horizontal=False
)

# Mode upload berubah berdasarkan kegiatan
is_archive_mode = kegiatan in ARCHIVE_KEGIATAN

# Dynamic key: berubah tiap kali SN atau kegiatan ganti -> auto clear uploader
uploader_key = f"uploader_{sn_input}_{kegiatan}"

if is_archive_mode:
    st.info(f"📦 Mode Archive — upload file log/RAR/ZIP untuk **{kegiatan}**")
    uploaded_files = st.file_uploader(
        f"Upload File Fault Recorder (RAR/ZIP/log/dll)",
        type=ARCHIVE_EXTENSIONS,
        accept_multiple_files=True,
        help="Bisa multiple file. File akan di-upload as-is (tidak di-extract). Format support: RAR, ZIP, TAR, GZ, 7Z, LOG, TXT, CSV",
        key=uploader_key
    )
else:
    uploaded_files = st.file_uploader(
        "Upload Foto Dokumentasi (bisa multiple)",
        type=IMAGE_EXTENSIONS,
        accept_multiple_files=True,
        help="Foto akan di-sortir otomatis berdasarkan timestamp EXIF. Tip: kalau di HP, upload bertahap max 10 foto.",
        key=uploader_key
    )

if uploaded_files:
    total_size_mb = sum(f.size for f in uploaded_files) / (1024 * 1024)
    file_label = "file" if is_archive_mode else "foto"
    st.info(f"📸 {len(uploaded_files)} {file_label} dipilih ({total_size_mb:.1f} MB)")
    
    if is_archive_mode:
        with st.expander("📋 List file"):
            for f in uploaded_files:
                size_mb = f.size / (1024 * 1024)
                st.write(f"- `{f.name}` ({size_mb:.1f} MB)")
    
    if total_size_mb > 100:
        st.warning(f"⚠️ Ukuran total {total_size_mb:.0f} MB cukup besar.")

catatan = st.text_area(
    "Catatan Tambahan (opsional)",
    placeholder="Misal: nama site, kondisi inverter, tanggal kejadian fault, dll",
    height=80
)

st.divider()
submit = st.button("🚀 Submit Upload", type="primary", use_container_width=True)

if submit:
    if not sn_input:
        st.error("❌ Serial Number wajib diisi")
        st.stop()
    
    valid_sn, msg_sn = validate_sn(sn_input)
    if not valid_sn:
        st.error(f"❌ {msg_sn}")
        st.stop()
    
    valid_inv, msg_inv = validate_inv_number(inv_number)
    if not valid_inv:
        st.error(f"❌ {msg_inv}")
        st.stop()
    
    if not uploaded_files:
        st.error("❌ Minimal upload 1 file")
        st.stop()
    
    progress = st.progress(0, text="Memulai...")
    
    try:
        progress.progress(5, text="Cek folder SN...")
        sn_folder_id = find_folder(service, sn_input, ROOT_FOLDER_ID)
        is_new_folder = False
        
        if not sn_folder_id:
            progress.progress(10, text="Buat folder baru + 6 subfolder kegiatan...")
            sn_folder_id = create_folder(service, sn_input, ROOT_FOLDER_ID)
            for folder_name in KEGIATAN_FOLDERS.values():
                create_folder(service, folder_name, sn_folder_id)
            is_new_folder = True
        
        target_folder_name = KEGIATAN_FOLDERS[kegiatan]
        target_folder_id = find_folder(service, target_folder_name, sn_folder_id)
        if not target_folder_id:
            target_folder_id = create_folder(service, target_folder_name, sn_folder_id)
        
        progress.progress(15, text="Update metadata...")
        try:
            current_metadata = load_metadata(service)
            current_metadata[sn_input] = inv_number
            save_metadata(service, current_metadata)
            get_metadata_cached.clear()
        except Exception as meta_err:
            st.warning(f"⚠️ Metadata update gagal: {meta_err} (upload tetap lanjut)")
        
        first_ts = None
        last_ts = None
        uploaded_count = 0
        failed_files = []
        existing_count = count_files_in_folder(service, target_folder_id)
        
        if is_archive_mode:
            progress.progress(25, text="Upload archive files...")
            
            for idx, f in enumerate(uploaded_files):
                try:
                    upload_time = datetime.now()
                    seq = str(existing_count + idx + 1).zfill(3)
                    date_str = upload_time.strftime('%Y-%m-%d')
                    time_str = upload_time.strftime('%H-%M-%S')
                    
                    original_name = f.name
                    ext = original_name.split('.')[-1].lower()
                    base_name = '.'.join(original_name.split('.')[:-1])[:40]
                    new_name = f"{seq}_{date_str}_{time_str}_{base_name}.{ext}"
                    
                    pct = 25 + int((idx + 1) / len(uploaded_files) * 65)
                    progress.progress(pct, text=f"Upload {idx + 1}/{len(uploaded_files)}: {new_name[:50]}...")
                    
                    upload_file_streaming(service, f, new_name, target_folder_id)
                    
                    if first_ts is None:
                        first_ts = upload_time
                    last_ts = upload_time
                    uploaded_count += 1
                    gc.collect()
                    
                except Exception as file_err:
                    failed_files.append((f.name, str(file_err)))
                    continue
        
        else:
            progress.progress(20, text="Baca timestamp foto...")
            file_meta = []
            for f in uploaded_files:
                ts = get_exif_timestamp_from_uploaded(f)
                file_meta.append({'file': f, 'timestamp': ts, 'original_name': f.name})
            
            file_meta.sort(key=lambda x: x['timestamp'])
            
            for idx, meta in enumerate(file_meta):
                try:
                    f = meta['file']
                    ts = meta['timestamp']
                    seq = str(existing_count + idx + 1).zfill(3)
                    date_str = ts.strftime('%Y-%m-%d')
                    time_str = ts.strftime('%H-%M-%S')
                    ext = meta['original_name'].split('.')[-1].lower()
                    new_name = f"{seq}_{date_str}_{time_str}.{ext}"
                    
                    pct = 25 + int((idx + 1) / len(file_meta) * 65)
                    progress.progress(pct, text=f"Upload {idx + 1}/{len(file_meta)}: {new_name}")
                    
                    upload_file_streaming(service, f, new_name, target_folder_id)
                    
                    if first_ts is None:
                        first_ts = ts
                    last_ts = ts
                    uploaded_count += 1
                    gc.collect()
                    
                except Exception as file_err:
                    failed_files.append((meta['original_name'], str(file_err)))
                    continue
        
        progress.progress(95, text="Update log...")
        if uploaded_count > 0:
            update_log(service, sn_folder_id, sn_input, inv_number, kegiatan, uploaded_count, first_ts, last_ts, catatan)
        
        folder_url = f"https://drive.google.com/drive/folders/{sn_folder_id}"
        status = "✅ FOLDER BARU DIBUAT" if is_new_folder else "⚠️ SN SUDAH ADA - File di-append"
        file_type = "file archive" if is_archive_mode else "foto"
        
        email_sent, email_msg = send_notification_email(
            sn_input, inv_number, kegiatan, uploaded_count, status, folder_url, first_ts, last_ts, catatan, file_type
        )
        
        progress.progress(100, text="Selesai!")
        
        if uploaded_count > 0 and not failed_files:
            st.success(f"✅ Upload berhasil! {uploaded_count} {file_type} ke-upload.")
        elif uploaded_count > 0 and failed_files:
            st.warning(f"⚠️ {uploaded_count} sukses, {len(failed_files)} gagal.")
            with st.expander("📋 File yang gagal"):
                for name, err in failed_files:
                    st.write(f"- **{name}**: {err}")
        else:
            st.error("❌ Semua file gagal di-upload.")
        
        col1, col2, col3 = st.columns(3)
        with col1:
            st.metric("File Berhasil", uploaded_count)
        with col2:
            st.metric("Inverter", inv_number)
        with col3:
            st.metric("Status Folder", "Baru" if is_new_folder else "Existing")
        
        st.markdown(f"**📁 [Buka Folder Google Drive]({folder_url})**")
        
        if email_sent:
            st.info("📧 Notifikasi email terkirim ke admin")
        else:
            st.warning(f"⚠️ Email notifikasi gagal: {email_msg}")
        
        if uploaded_count > 0:
            with st.expander("📋 Detail Submission"):
                st.write(f"**Serial Number:** `{sn_input}`")
                st.write(f"**Inverter Number:** `{inv_number}`")
                st.write(f"**Kegiatan:** {kegiatan}")
                st.write(f"**Mode:** {'Archive' if is_archive_mode else 'Image'}")
                st.write(f"**Jumlah file:** {uploaded_count}")
                if first_ts and last_ts:
                    st.write(f"**Range waktu:** {first_ts.strftime('%Y-%m-%d %H:%M:%S')} → {last_ts.strftime('%H:%M:%S')}")
                if catatan:
                    st.write(f"**Catatan:** {catatan}")
        
    except Exception as e:
        progress.empty()
        st.error(f"❌ Error: {str(e)}")
        st.exception(e)

st.divider()
st.caption("Sungrow Power Supply Co. Ltd. — Technical Service")
