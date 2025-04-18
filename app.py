from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit
import threading
import RPi.GPIO as GPIO
import smbus
import time
import atexit
import json
import os
from datetime import datetime

# start flask and socketio
app = Flask(__name__)
socketio = SocketIO(app)

# connect to adc via i2c using smbus
bus = smbus.SMBus(1)
pcf8591_address = 0x48

# Preset data
analog_min = 36
analog_max = 53

Relay_Ch1 = 26
Relay_Ch2 = 20
Relay_Ch3 = 21

min_percentage = 20
max_percentage = 80

# Setup GPIO and Relays
GPIO.setwarnings(False)
GPIO.setmode(GPIO.BCM)

GPIO.setup(Relay_Ch1, GPIO.OUT)
GPIO.setup(Relay_Ch2, GPIO.OUT)
GPIO.setup(Relay_Ch3, GPIO.OUT)
pin_on = GPIO.HIGH
pin_off = GPIO.LOW


stop_thread = threading.Event()

NUM_READINGS = 5
analog_values = [0] * NUM_READINGS
sensor_disconnected = False
percentage = 0
average_value = 0
GPIO.output(Relay_Ch3, pin_off)

zones = []  # loaded config
sensor_readings = {}  # per sensor id: { "moisture": %, "disconnected": True/False }

CONFIG_FILE = 'zones.json'

def load_config():
    """
    Loads the zones configuration from the JSON file.
    Returns a list of zone dictionaries.
    If the file does not exist or is invalid, returns an empty list.
    """
    if not os.path.exists(CONFIG_FILE):
        print(f"[load_config] Config file '{CONFIG_FILE}' not found. Using empty config.")
        return []

    try:
        with open(CONFIG_FILE, 'r') as f:
            zones = json.load(f)
            # Validate basic structure
            for zone in zones:
                if 'name' not in zone or 'valve' not in zone or 'sensors' not in zone or 'active' not in zone or 'watering_modes' not in zone:
                    print(f"[load_config] Warning: Invalid zone structure detected, zone: {zone}")
                    return []
            print(f"[load_config] Successfully loaded {len(zones)} zones from config.")
            return zones

    except json.JSONDecodeError as e:
        print(f"[load_config] JSON decode error: {e}")
        return []
    except Exception as e:
        print(f"[load_config] Unexpected error loading config: {e}")
        return []

def overwrite_config(all_zones):
    """
    Completely overwrites the config file with the provided list of zones.
    all_zones should be a list of properly structured zone dictionaries.
    """
    try:
        with open(CONFIG_FILE, 'w') as f:
            json.dump(all_zones, f, indent=4)
        print(f"[overwrite_config] Successfully overwrote config with {len(all_zones)} zones.")
    except Exception as e:
        print(f"[overwrite_config] Failed to overwrite config: {e}")

def start_sensor_thread():
    sensor_thread = threading.Thread(target=sensor_loop, daemon=True)
    sensor_thread.start()

def start_controller_thread():
    controller_thread = threading.Thread(target=controller_loop, daemon=True)
    controller_thread.start()

def start_led_thread():
    led_thread = threading.Thread(target=led_loop, daemon=True)
    led_thread.start()

def start_watchdog_thread():
    watchdog_thread = threading.Thread(target=watchdog_loop, daemon=True)
    watchdog_thread.start()

@socketio.on('connect')
def handle_connect():
    print('Client connected')
    emit('sensor_update', {'sensors': sensor_readings})

def map_value(value, from_min, from_max, to_min, to_max):
    return (value - from_min) * (to_max - to_min) / (from_max - from_min) + (to_min)

def read_channel(channel):
    control_byte = 0x40 | (channel & 0x03)
    bus.write_byte(pcf8591_address, control_byte)
    discard = bus.read_byte(pcf8591_address)
    fresh_data = bus.read_byte(pcf8591_address)
    return fresh_data

def sensor_loop():
    global sensor_readings
    global temp_reading

    while True:
        try:
            # Read Sensor 1 (AIN0)
            raw_val_0 = read_channel(0)
            moisture_0 = map_value(raw_val_0, analog_min, analog_max, 100, 0)
            disconnected_0 = raw_val_0 < (analog_min - 5) or raw_val_0 > (analog_max + 5)

            # Read Thermistor (AIN1)
            raw_temp = read_channel(1)
            # (Optional: you can later convert this to Celsius/Fahrenheit)
            temp_reading = raw_temp

            # Read Sensor 2 (AIN2)
            raw_val_2 = read_channel(2)
            moisture_2 = map_value(raw_val_2, analog_min, analog_max, 100, 0)
            disconnected_2 = raw_val_2 < (analog_min - 5) or raw_val_2 > (analog_max + 5)

            # Read Sensor 3 (AIN3) (optional, future)
            raw_val_3 = read_channel(3)
            moisture_3 = map_value(raw_val_3, analog_min, analog_max, 100, 0)
            disconnected_3 = raw_val_3 < (analog_min - 5) or raw_val_3 > (analog_max + 5)

            # Store readings
            sensor_readings = {
                1: {'moisture': max(0, min(100, moisture_0)), 'disconnected': disconnected_0},
                2: {'moisture': max(0, min(100, moisture_2)), 'disconnected': disconnected_2},
                3: {'moisture': max(0, min(100, moisture_3)), 'disconnected': disconnected_3}
            }

            # Emit updates to frontend if needed
            socketio.emit('sensor_update', {
                'sensors': sensor_readings,
                'temp_raw': temp_reading
            })

            # Debugging (Optional)
            print(f"Sensor 1 Moisture: {moisture_0:.2f}% {'(Disconnected)' if disconnected_0 else ''}")
            print(f"Thermistor Raw: {temp_reading}")
            print(f"Sensor 2 Moisture: {moisture_2:.2f}% {'(Disconnected)' if disconnected_2 else ''}")
            print(f"Sensor 3 Moisture: {moisture_3:.2f}% {'(Disconnected)' if disconnected_3 else ''}")
            print("----")

            time.sleep(2)  # Wait between sweeps

        except Exception as e:
            print(f"[sensor_loop] Error: {e}")
            time.sleep(2)

def controller_loop():
    global zones
    global sensor_readings

    print("[controller_loop] Started")
    
    while True:
        now = datetime.now()

        for zone in zones:
            if not zone['active']:
                continue

            zone_name = zone['name']
            valve = zone['valve']
            sensors = zone['sensors']
            watering_modes = zone['watering_modes']

            should_water = False
            reason = ""

            # Parse last watered time
            last_watered_str = zone.get('last_watered', None)
            last_watered = None
            if last_watered_str:
                try:
                    last_watered = datetime.fromisoformat(last_watered_str)
                except Exception:
                    last_watered = None

            # ---- Sensor Based Watering ----
            if watering_modes.get('sensor_based', {}).get('enabled', False):
                threshold = watering_modes['sensor_based']['threshold_percentage']

                for sensor_id in sensors:
                    reading = sensor_readings.get(sensor_id, None)
                    if reading and not reading['disconnected']:
                        if reading['moisture'] < threshold:
                            should_water = True
                            reason = f"Sensor {sensor_id} below threshold {threshold}%"
                            break

            # ---- Timer Based Watering ----
            if not should_water and watering_modes.get('timer_based', {}).get('enabled', False):
                interval_hours = watering_modes['timer_based']['interval_hours']

                if last_watered is None or (now - last_watered).total_seconds() >= interval_hours * 3600:
                    should_water = True
                    reason = f"Timer elapsed ({interval_hours}h)"

            # ---- Scheduled Watering ----
            if not should_water and watering_modes.get('scheduled', {}).get('enabled', False):
                scheduled_times = watering_modes['scheduled']['times']

                for sched_time in scheduled_times:
                    sched_hour, sched_minute = map(int, sched_time.split(':'))
                    scheduled_dt = now.replace(hour=sched_hour, minute=sched_minute, second=0, microsecond=0)

                    if (last_watered is None or scheduled_dt > last_watered) and scheduled_dt <= now:
                        should_water = True
                        reason = f"Scheduled watering at {sched_time}"
                        break

            # ---- Actually Water if Needed ----
            if should_water:
                print(f"[controller_loop] Watering Zone '{zone_name}' because: {reason}")
                water_zone(valve, sensors)

                # Always update last watered
                zone['last_watered'] = now.isoformat()
                overwrite_config(zones)
                print(f"[controller_loop] Updated last_watered for zone '{zone_name}' to {zone['last_watered']}")

        time.sleep(5)

def water_zone(valve, sensors):
    # Valve to GPIO pin mapping (adjust as needed)
    valve_gpio_map = {
        1: Relay_Ch1,
        2: Relay_Ch2,
        3: Relay_Ch3
    }
    pin = valve_gpio_map.get(valve, None)
    if pin is None:
        print(f"[water_zone] Invalid valve {valve}")
        return

    GPIO.output(pin, pin_on)
    watering_start = time.time()
    MAX_WATERING_SECONDS = 120  # Example 2 minutes max watering per zone

    try:
        while True:
            # Recheck sensor status
            all_wet = True
            for sensor_id in sensors:
                reading = sensor_readings.get(sensor_id, None)
                if reading and not reading['disconnected']:
                    if reading['moisture'] < max_percentage:
                        all_wet = False
                        break

            if all_wet:
                print("[water_zone] All sensors wet, stopping watering.")
                break

            if time.time() - watering_start > MAX_WATERING_SECONDS:
                print("[water_zone] Max watering time reached, stopping watering.")
                break

            time.sleep(2)  # check every 2 seconds
    finally:
        GPIO.output(pin, pin_off)
        print("[water_zone] Valve turned OFF")

def led_loop():
    pass
def watchdog_loop():
    pass

def cleanup_gpio():
    GPIO.cleanup()

atexit.register(cleanup_gpio)

@app.route('/')
def index():
    try:
        with open('zones.json', 'r') as f:
            zones_data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        zones_data = []
    return render_template('index.html', zones_data=zones_data)

@app.route('/manual_start_zone', methods=['POST'])
def manual_start_zone():
    valve = int(request.form['valve'])
    valve_gpio_map = {
        1: Relay_Ch1,
        2: Relay_Ch2,
        3: Relay_Ch3
    }
    pin = valve_gpio_map.get(valve, None)
    if pin is not None:
        GPIO.output(pin, pin_on)
        return f"Valve {valve} turned ON manually!", 200
    else:
        return "Invalid valve!", 400

@app.route('/manual_stop_zone', methods=['POST'])
def manual_stop_zone():
    valve = int(request.form['valve'])
    valve_gpio_map = {
        1: Relay_Ch1,
        2: Relay_Ch2,
        3: Relay_Ch3
    }
    pin = valve_gpio_map.get(valve, None)
    if pin is not None:
        GPIO.output(pin, pin_off)
        return f"Valve {valve} turned OFF manually!", 200
    else:
        return "Invalid valve!", 400

@app.route('/save_zones', methods=['POST'])
def save_zones():
    try:
        zones_data = request.get_json()
        if not isinstance(zones_data, list):
            return "Invalid data format: Expected a list.", 400

        overwrite_config(zones_data)
        global zones
        zones = zones_data  # Update in-memory zones for controller_loop
        print("[save_zones] Zones updated successfully")
        return 'Zones saved successfully.', 200

    except Exception as e:
        print(f"[save_zones] Error: {e}")
        return 'Failed to save zones.', 500

if __name__ == '__main__':
    zones = load_config()
    start_sensor_thread()
    start_controller_thread()
    start_led_thread()
    start_watchdog_thread()
    socketio.run(app, host='0.0.0.0', allow_unsafe_werkzeug=True)