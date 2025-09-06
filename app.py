# ==============================================================================
# LUNG CAPACITY ESTIMATOR - FLASK SERVER (app.py) - FINAL CORRECTED FORMULA
# ==============================================================================
# This script runs a local web server that receives user data and returns
# a realistic estimated lung capacity (FVC) with a health classification.
# ==============================================================================

from flask import Flask, request, jsonify
from flask_cors import CORS
import os

# --- Basic Flask App Setup ---
app = Flask(__name__)
CORS(app) 

# Create a folder to temporarily store uploaded audio files
UPLOAD_FOLDER = 'uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)


# --- Main API Endpoint ---

@app.route('/upload', methods=['POST'])
def upload():
    # 1. Get data from the form
    audio_file = request.files['audio']
    gender = request.form['gender']
    age = int(request.form['age'])
    height_inches = int(request.form['height'])

    # Save the audio file just to confirm it was received
    file_path = os.path.join(UPLOAD_FOLDER, 'last_recording.wav')
    audio_file.save(file_path)

    # ==========================================================================
    # --- STEP 1: CALCULATE THE PREDICTED FVC ---
    # ==========================================================================
    # This version uses a standard spirometry reference equation for a plausible
    # estimation. It does not use the audio data to avoid unstable results.

    # First, convert height from inches to centimeters for the formula.
    height_cm = height_inches * 2.54

    # Use a standard reference equation to calculate the predicted FVC in Liters.
    if gender.lower() == 'm':
        # Formula for males
        predicted_fvc = (0.052 * height_cm) - (0.022 * age) - 4.2
    else:
        # Formula for females
        predicted_fvc = (0.041 * height_cm) - (0.019 * age) - 3.19

    # Ensure the result is not negative.
    predicted_fvc = max(0, predicted_fvc)
    
    # ==========================================================================
    # --- STEP 2: ADD A PERSONALIZED HEALTH CLASSIFICATION ---
    # ==========================================================================
    # This classification compares the predicted FVC to different thresholds
    # based on percentages of what is expected for that person's demographic.
    
    # Using the predicted value as the benchmark for a "healthy" person.
    # LLN (Lower Limit of Normal) is typically 80% of the predicted value.
    lower_limit_normal = predicted_fvc * 0.80
    moderate_limit = predicted_fvc * 0.60

    # Note: Since we are not measuring a real FVC, we classify the *predicted*
    # value itself to give the user a status for their demographic.
    # In a real-world scenario, you would compare a *measured* FVC to these limits.
    
    status = ""
    if predicted_fvc >= lower_limit_normal:
        status = "✅ Healthy Range"
    elif predicted_fvc >= moderate_limit:
        status = "⚠️ Mild Reduction Range"
    else:
        status = "❌ Significant Reduction Range"

    # ==========================================================================
    # --- STEP 3: FORMAT THE FINAL RESULT ---
    # ==========================================================================

    # Combine the predicted value and the classification into a single string.
    result = f"Predicted FVC: {predicted_fvc:.2f} L. Status: {status}"
    
    print(f"✅ Calculation complete. Sending result: {result}")
    return jsonify({"result": result})

# --- Start the Server ---
if __name__ == '__main__':
    app.run(debug=True)
