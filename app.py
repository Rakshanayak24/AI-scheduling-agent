import streamlit as st
import pandas as pd
from datetime import datetime, timedelta
import os, shutil

# -------------------
# Setup
# -------------------
st.set_page_config(page_title="AI Scheduling Agent", page_icon="🩺")

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
os.makedirs(DATA_DIR, exist_ok=True)

PATIENTS_CSV = os.path.join(DATA_DIR, "patients.csv")
SCHEDULES_XLSX = os.path.join(DATA_DIR, "doctor_schedules.xlsx")
APPTS_XLSX = os.path.join(DATA_DIR, "appointments.xlsx")
OUTBOX_DIR = os.path.join(DATA_DIR, "outbox")
INTAKE_FORM = os.path.join(os.path.dirname(__file__), "New Patient Intake Form.pdf")

os.makedirs(OUTBOX_DIR, exist_ok=True)

# -------------------
# Data Loaders
# -------------------
@st.cache_data
def load_patients():
    df = pd.read_csv(PATIENTS_CSV)
    if "is_returning" in df.columns:
        df["is_returning"] = df["is_returning"].astype(str).str.strip().str.lower().isin(["1", "true", "yes"])
    return df

@st.cache_data
def load_schedules_cached():
    """Load all sheets into a dict (cached). Note: APIs that need freshest data should read file directly."""
    book = {}
    xls = pd.ExcelFile(SCHEDULES_XLSX)
    for sheet in xls.sheet_names:
        df = xls.parse(sheet)
        # normalize booked column if present
        if "booked" in df.columns:
            df["booked"] = df["booked"].fillna(False).astype(bool)
        book[sheet] = df
    return book

def save_schedules(book):
    """Write schedule dict (doctor -> df) to the Excel file."""
    with pd.ExcelWriter(SCHEDULES_XLSX, engine="openpyxl") as writer:
        for doc, df in book.items():
            df.to_excel(writer, sheet_name=doc, index=False)

def append_appointment(row):
    if os.path.exists(APPTS_XLSX):
        appts = pd.read_excel(APPTS_XLSX)
    else:
        appts = pd.DataFrame()
    appts = pd.concat([appts, pd.DataFrame([row])], ignore_index=True)
    appts.to_excel(APPTS_XLSX, index=False)

# -------------------
# Utilities
# -------------------
def find_patient(df, name, dob_str):
    parts = name.strip().split()
    if len(parts) < 2:
        return None
    fn, ln = parts[0].lower(), parts[-1].lower()

    # Normalize CSV DOB
    df = df.copy()
    if "dob" in df.columns:
        df["dob"] = pd.to_datetime(df["dob"], errors="coerce").dt.date

    # Parse input DOB
    try:
        dob = datetime.strptime(dob_str, "%Y-%m-%d").date()
    except:
        dob = None

    candidates = df[
        (df["first_name"].str.lower() == fn)
        & (df["last_name"].str.lower() == ln)
    ]
    if dob is not None and "dob" in df.columns:
        candidates = candidates[candidates["dob"] == dob]

    if candidates.empty:
        return None
    return candidates.iloc[0]

def get_slots(doctor, date_str):
    """
    Read the doctor's sheet fresh from Excel and return available (not booked) slots for date_str.
    This ensures we show the most recent availability.
    """
    try:
        df = pd.read_excel(SCHEDULES_XLSX, sheet_name=doctor)
    except Exception:
        return pd.DataFrame()  # no sheet or file

    # Normalize booked column
    if "booked" in df.columns:
        df["booked"] = df["booked"].fillna(False).astype(bool)
    else:
        df["booked"] = False

    # Match rows exactly by date string (assumes schedule 'date' column stores ISO strings)
    available = df[(df["date"] == date_str) & (df["booked"] == False)]
    return available.reset_index(drop=True)

def reserve_slot(doctor, date_str, start_time, patient_id):
    """
    Atomically reserve a slot by reloading the doctor's sheet, verifying the slot is still free,
    updating it to booked, and saving back to the Excel workbook.
    Returns True if reservation succeeded, False otherwise.
    """
    # Load the whole workbook so we can write it back safely
    try:
        xls = pd.ExcelFile(SCHEDULES_XLSX)
    except Exception:
        return False

    updated = False
    # We'll build a dict of modified sheets and then write back all sheets
    sheets = {}
    for sheet_name in xls.sheet_names:
        df_sheet = xls.parse(sheet_name)
        # normalize booked col
        if "booked" in df_sheet.columns:
            df_sheet["booked"] = df_sheet["booked"].fillna(False).astype(bool)
        else:
            df_sheet["booked"] = False

        if sheet_name == doctor:
            # find the exact row
            mask = (df_sheet["date"] == date_str) & (df_sheet["start_time"] == start_time)
            if mask.sum() == 0:
                # slot not present in schedule (invalid)
                sheets[sheet_name] = df_sheet
                continue
            # if any matched row is already booked, cannot reserve
            if df_sheet.loc[mask, "booked"].any():
                # leave file unchanged and indicate failure
                return False
            # mark as booked and set patient_id
            df_sheet.loc[mask, "booked"] = True
            # ensure patient_id column exists
            if "patient_id" not in df_sheet.columns:
                df_sheet["patient_id"] = ""
            df_sheet.loc[mask, "patient_id"] = patient_id
            updated = True

        sheets[sheet_name] = df_sheet

    # If we reached here and updated is True, write back all sheets
    if updated:
        with pd.ExcelWriter(SCHEDULES_XLSX, engine="openpyxl") as writer:
            for sheet_name, df_sheet in sheets.items():
                df_sheet.to_excel(writer, sheet_name=sheet_name, index=False)
        return True

    return False

def send_confirmation(patient, appt):
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    email_path = os.path.join(OUTBOX_DIR, f"email_{ts}_{patient['first_name']}_{patient['last_name']}.txt")
    sms_path = os.path.join(OUTBOX_DIR, f"sms_{ts}_{patient['first_name']}_{patient['last_name']}.txt")
    with open(email_path, "w") as f:
        f.write(f"""Subject: Appointment Confirmed - {appt['date']} {appt['start_time']}

Hi {patient['first_name']},

Your appointment with {appt['doctor']} is confirmed.
Location: {appt['location']}
Date: {appt['date']} at {appt['start_time']} (Duration: {appt['duration_min']} min)

Please find the intake form attached in a separate email.

- Scheduling Agent
""")
    with open(sms_path, "w") as f:
        f.write(f"CONFIRMED: {appt['date']} {appt['start_time']} with {appt['doctor']} at {appt['location']}")
    return email_path, sms_path

def send_form(patient):
    if not os.path.exists(INTAKE_FORM):
        return ""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = os.path.join(OUTBOX_DIR, f"intake_form_{ts}_{patient['first_name']}_{patient['last_name']}.pdf")
    shutil.copy(INTAKE_FORM, dest)
    return dest

# -------------------
# Streamlit Chat UI
# -------------------
st.title("🩺 AI Scheduling Agent")
st.caption("Greeting → Lookup → Smart Scheduling → Confirmation → Excel Export → Form → Reminders (simulated)")

patients = load_patients()
# we keep a cached view of schedules for admin downloads (but when checking availability/reserving we read file fresh)
schedules_cached = load_schedules_cached()

if "messages" not in st.session_state:
    st.session_state.messages = []
if "context" not in st.session_state:
    st.session_state.context = {"stage": "greet"}

for m in st.session_state.messages:
    with st.chat_message(m["role"]):
        st.markdown(m["content"])

def bot_say(text):
    st.session_state.messages.append({"role": "assistant", "content": text})
    with st.chat_message("assistant"):
        st.markdown(text)

# -------------------
# Conversation Flow
# -------------------
ctx = st.session_state.context

if ctx["stage"] == "greet":
    bot_say("Hello! I'm your clinic assistant. May I have your *full name*, *DOB (YYYY-MM-DD)*, preferred *doctor* (Dr_Sharma/Dr_Iyer), and *location*?")
    ctx["stage"] = "collect"

user_input = st.chat_input("Type here…")
if user_input:
    st.session_state.messages.append({"role": "user", "content": user_input})
    with st.chat_message("user"):
        st.markdown(user_input)

    # Restart command
    if user_input.strip().lower() == "restart":
        st.session_state.messages = []
        st.session_state.context = {"stage": "greet"}
        st.rerun()

    if ctx["stage"] == "collect":
        def extract(key):
            for token in user_input.split(","):
                if key.lower() in token.lower():
                    return token.split("=", 1)[-1].strip()
            return None

        name = extract("name") or user_input.split(",")[0].strip()
        dob = extract("dob")
        doctor = extract("doctor") or "Dr_Sharma"
        location = extract("location") or "Bangalore - Indiranagar"
        ctx.update({"name": name, "dob": dob, "doctor": doctor, "location": location})

        patient = find_patient(patients, name, dob or "")
        ctx["patient_found"] = patient is not None

        if patient is None:
            # Immediately register new patient
            dfp = pd.read_csv(PATIENTS_CSV)
            new_id = 1 if dfp.empty else int(dfp["patient_id"].max()) + 1
            first, last = (name.split()[0], name.split()[-1])
            new_row = {
                "patient_id": new_id,
                "first_name": first,
                "last_name": last,
                "dob": dob,
                "email": f"{first.lower()}.{last.lower()}@example.com",
                "phone": "9000000000",
                "is_returning": False,
                "preferred_doctor": doctor,
                "insurance_company": "",
                "member_id": "",
                "group_number": "",
                "past_visits_count": 0,
            }
            dfp = pd.concat([dfp, pd.DataFrame([new_row])], ignore_index=True)
            dfp.to_csv(PATIENTS_CSV, index=False)
            patients = load_patients()  # refresh cached patients
            ctx["patient_id"] = new_id
            ctx["new_patient"] = True
            bot_say("I couldn't find you in our records — you're now registered as a new patient. Duration will be **60 min**. Please provide your **insurance**: carrier, member_id, group_number.")
        else:
            returning = bool(patient["is_returning"])
            ctx["new_patient"] = not returning

            # update CSV so next visit recognized as returning
            dfp = pd.read_csv(PATIENTS_CSV)
            dfp.loc[dfp["patient_id"] == patient["patient_id"], "is_returning"] = True
            dfp.to_csv(PATIENTS_CSV, index=False)
            patients = load_patients()  # refresh

            if returning:
                bot_say(f"Welcome back, **{patient['first_name']}**! I detected you as a *returning* patient. Duration will be **30 min**. Please confirm/update your **insurance**: carrier, member_id, group_number.")
            else:
                bot_say(f"Hello **{patient['first_name']}**! I detected you as a *new patient*. Duration will be **60 min**. Please confirm/update your **insurance**: carrier, member_id, group_number.")
            ctx["patient_id"] = int(patient["patient_id"])
        ctx["stage"] = "insurance"

    elif ctx["stage"] == "insurance":
        parts = [p.strip() for p in user_input.split(",")]
        carrier = parts[0] if len(parts) > 0 else "Unknown"
        member_id = parts[1] if len(parts) > 1 else "Unknown"
        group_number = parts[2] if len(parts) > 2 else "Unknown"
        ctx.update({"carrier": carrier, "member_id": member_id, "group_number": group_number})

        # show slots reading fresh from file for each date to avoid stale data
        today = datetime.now().date()
        dates = [today.isoformat(), (today + timedelta(days=1)).isoformat(), (today + timedelta(days=2)).isoformat()]
        slots = pd.DataFrame()
        for d in dates:
            s = get_slots(ctx["doctor"], d)
            if not s.empty:
                s = s.assign(slot_id=[f"{i}" for i in range(len(s))])
                s["label"] = s["date"] + " " + s["start_time"] + " (" + s["location"] + ")"
                slots = pd.concat([slots, s], ignore_index=True)
        if slots.empty:
            bot_say("Hmm, I don't see open slots in the next 3 days for that doctor. Try another doctor or date.")
            ctx["stage"] = "collect"
        else:
            ctx["slots"] = slots
            options = "\n".join([f"- **{i}**. {row['label']}" for i, row in slots.iterrows()])
            bot_say(f"### Available slots (next 3 days):\n{options}\n\n👉 Reply with the **number** of your preferred slot.")
            ctx["stage"] = "pick_slot"

    elif ctx["stage"] == "pick_slot":
        try:
            choice = int(user_input.strip().split()[0])
        except:
            bot_say("Please reply with a valid slot number (e.g., `3`).")
            st.stop()

        slots = ctx["slots"]
        if choice < 0 or choice >= len(slots):
            bot_say("That number is out of range. Try again.")
            st.stop()

        selected = slots.iloc[choice]

        # ensure patient exists and save/update insurance immediately
        if ctx.get("patient_id") is None:
            # create a new patient (should rarely happen because we created earlier)
            dfp = pd.read_csv(PATIENTS_CSV)
            new_id = 1 if dfp.empty else int(dfp["patient_id"].max()) + 1
            first, last = (ctx["name"].split()[0], ctx["name"].split()[-1])
            new_row = {
                "patient_id": new_id,
                "first_name": first,
                "last_name": last,
                "dob": ctx["dob"],
                "email": f"{first.lower()}.{last.lower()}@example.com",
                "phone": "9000000000",
                "is_returning": False,
                "preferred_doctor": ctx["doctor"],
                "insurance_company": ctx["carrier"],
                "member_id": ctx["member_id"],
                "group_number": ctx["group_number"],
                "past_visits_count": 0,
            }
            dfp = pd.concat([dfp, pd.DataFrame([new_row])], ignore_index=True)
            dfp.to_csv(PATIENTS_CSV, index=False)
            ctx["patient_id"] = new_id
            patient = new_row
        else:
            dfp = pd.read_csv(PATIENTS_CSV)
            patient = dfp[dfp["patient_id"] == ctx["patient_id"]].iloc[0].to_dict()
            # update insurance fields in CSV
            for k in ["insurance_company", "member_id", "group_number"]:
                dfp.loc[dfp["patient_id"] == ctx["patient_id"], k] = ctx["carrier"] if k == "insurance_company" else (ctx["member_id"] if k == "member_id" else ctx["group_number"])
            dfp.to_csv(PATIENTS_CSV, index=False)

        # Attempt to reserve the slot atomically (reads/writes Excel file)
        success = reserve_slot(ctx["doctor"], selected["date"], selected["start_time"], ctx["patient_id"])
        if not success:
            bot_say("Oops, that slot just got booked by someone else. Please pick another number.")
            st.stop()

        duration = 60 if ctx.get("new_patient", True) else 30
        appt = {
            "appointment_id": f"A{int(datetime.now().timestamp())}",
            "patient_id": ctx["patient_id"],
            "patient_name": ctx["name"],
            "dob": ctx["dob"],
            "doctor": ctx["doctor"],
            "location": selected["location"],
            "date": selected["date"],
            "start_time": selected["start_time"],
            "end_time": selected["end_time"],
            "duration_min": duration,
            "insurance_company": ctx["carrier"],
            "member_id": ctx["member_id"],
            "group_number": ctx["group_number"],
            "status": "CONFIRMED",
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "reason_if_cancelled": "",
            "forms_sent_path": "",
            "confirmation_email_path": "",
            "sms_log_path": "",
        }

        email_path, sms_path = send_confirmation(patient, appt)
        form_path = send_form(patient)
        appt["confirmation_email_path"] = email_path
        appt["sms_log_path"] = sms_path
        appt["forms_sent_path"] = form_path

        append_appointment(appt)

        bot_say(f"✅ **Booked!** {appt['date']} {appt['start_time']} with **{appt['doctor']}** at **{appt['location']}**.\n\nI've sent a confirmation email/SMS and dispatched the intake form.")

        reminders_csv = os.path.join(OUTBOX_DIR, f"reminders_{appt['appointment_id']}.csv")
        now = datetime.now()
        plan = pd.DataFrame([
            {"when": "T-72h", "action": "reminder_email", "status": "pending", "scheduled_at": (now + timedelta(hours=1)).isoformat(timespec="seconds")},
            {"when": "T-24h", "action": "reminder_email_sms_form_check", "status": "pending", "scheduled_at": (now + timedelta(hours=2)).isoformat(timespec="seconds")},
            {"when": "T-2h", "action": "reminder_sms_confirm_or_cancel", "status": "pending", "scheduled_at": (now + timedelta(hours=3)).isoformat(timespec="seconds")},
        ])
        plan.to_csv(reminders_csv, index=False)

        # refresh cached schedules for admin view
        schedules_cached = load_schedules_cached()

        ctx["stage"] = "done"

    elif ctx["stage"] == "done":
        bot_say("🎉 You're all set! If you want to book another appointment, just type `restart`.")

# -------------------
# Sidebar Admin
# -------------------
st.sidebar.header("Admin")
if os.path.exists(APPTS_XLSX):
    st.sidebar.download_button("Download Appointments Excel", data=open(APPTS_XLSX, "rb"), file_name="appointments.xlsx")
st.sidebar.download_button("Download Patients CSV", data=open(PATIENTS_CSV, "rb"), file_name="patients.csv")
st.sidebar.download_button("Download Schedules Excel", data=open(SCHEDULES_XLSX, "rb"), file_name="doctor_schedules.xlsx")

st.sidebar.markdown("**Outbox Files** (emails/SMS/forms):")
if os.path.exists(OUTBOX_DIR):
    files = sorted(os.listdir(OUTBOX_DIR))[-10:]
    for f in files:
        st.sidebar.write(f"- {f}")




