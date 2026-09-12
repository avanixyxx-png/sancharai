============================================================
   📂 CSV TEMPLATES — Ready-to-upload customer lists
============================================================

How to use:
  1. Open localhost:8000
  2. Click the matching industry → use case (auto-fills greeting)
  3. Click 💾 Save All Settings
  4. Scroll to "🚀 Start AI Calls" → Upload the matching CSV below
  5. Edit the rows to add YOUR real customer data first!

⚠️ Replace sample numbers with your Twilio-VERIFIED numbers
   (https://console.twilio.com/us1/develop/phone-numbers/manage/verified)

============================================================
   📋 INDEX — File → Industry → Use case
============================================================

🛍️  E-COMMERCE / RETAIL
  01_ecom_order_confirmation.csv      → 📦 Order confirmation
  02_ecom_abandoned_cart.csv          → 🛒 Abandoned cart recovery
  03_ecom_customer_feedback.csv       → ⭐ Customer feedback
  04_ecom_cod_payment_reminder.csv    → 💳 COD payment reminder

🏥  HEALTHCARE / CLINIC
  05_health_appointment_booking.csv   → 📅 Appointment booking
  06_health_appointment_reminder.csv  → 🔔 Appointment reminder
  07_health_medication_reminder.csv   → 💊 Medication reminder
  08_health_lab_report.csv            → 🧪 Lab report ready

🍔  RESTAURANT / FOOD DELIVERY
  09_food_take_order.csv              → 📋 Take order
  10_food_order_status.csv            → ✅ Order status update
  11_food_table_reservation.csv       → 🪑 Table reservation

🏠  REAL ESTATE
  12_realestate_property_inquiry.csv  → 🏘️  Property inquiry follow-up
  13_realestate_site_visit.csv        → 🗓️  Schedule site visit

🎓  EDUCATION / COACHING
  14_edu_admission_inquiry.csv        → 📚 Admission inquiry
  15_edu_class_reminder.csv           → 📝 Class reminder
  16_edu_fee_reminder.csv             → 💵 Fee payment reminder

💰  FINANCE / BANKING / INSURANCE
  17_finance_emi_reminder.csv         → 💳 EMI payment reminder
  18_finance_insurance_renewal.csv    → 🛡️  Insurance renewal
  19_finance_loan_offer.csv           → 🎁 Loan offer

🏨  HOTEL / TRAVEL
  20_hotel_booking_confirmation.csv   → 🛏️  Booking confirmation
  21_hotel_post_stay_feedback.csv     → 🌟 Post-stay feedback

🛠️   SERVICE / MAINTENANCE
  22_service_appointment_reminder.csv → 🧹 Service appointment reminder
  23_service_followup.csv             → 🔧 Service follow-up

👔  HR / RECRUITMENT
  24_hr_candidate_screening.csv       → 📞 Candidate screening
  25_hr_interview_scheduling.csv      → 🗓️  Interview scheduling

📊  SURVEY / RESEARCH
  26_survey_satisfaction.csv          → 📋 Customer satisfaction survey
  27_survey_nps.csv                   → 🏆 NPS score collection

============================================================
   🏷️  PLACEHOLDERS YOU CAN USE IN GREETING / KNOWLEDGE
============================================================

  {name}                  Customer first name
  {phone}                 Phone number
  {order_id}              Order / booking / policy ID
  {product}               Product / course / room / role
  {amount}                Amount in rupees
  {exp_delivery_date}     Delivery / due date
  {exp_delivery_time}     Time slot
  {payment_mode}          COD / Prepaid UPI / Card
  {address}               Customer address
  {appointment_date}      For appointments / interviews
  {appointment_time}      For appointments / interviews
  {doctor}                Doctor / faculty / interviewer
  {info}                  Free-text catch-all

You can also add your own columns (e.g. policy_id) — just use
{policy_id} in the greeting and it will auto-fill.

============================================================
   📝 TIPS
============================================================

✅ The first column MUST be "phone" with country code (+91...)
✅ Header names must be lowercase, no spaces (use order_id NOT "Order ID")
✅ Leave a column blank if not needed — just commas, no value
✅ Each row = 1 customer = 1 personalized AI call
✅ All 27 CSV files use these 3 sample customers (Avani, Rahul, Priya)
   — replace them with YOUR customer data before calling
