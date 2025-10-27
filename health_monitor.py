import serial
import serial.tools.list_ports
import time
import csv
from datetime import datetime
import threading
import sys
import os
import requests

class HealthMonitor:
    def __init__(self, port=None, baudrate=9600):
        self.port = port
        self.baudrate = baudrate
        self.ser = None
        self.running = False
        # Use fixed filename to continue sessions
        self.csv_filename = "health_data_current.csv"
        self.current_patient_number = 0  # Will be updated from CSV or Arduino
        self.last_bpm = None
        self.last_heartbeat = None
        self.connection_lost = False
        
        # ThingSpeak Configuration
        self.thingspeak_enabled = True
        self.thingspeak_write_key = "BLXH6A2G9LFCBWI0"  # Your Write API Key
        self.thingspeak_channel_id = "3132500"          # Your Channel ID
        self.thingspeak_url = f"https://api.thingspeak.com/update?api_key={self.thingspeak_write_key}"
        
    def send_to_thingspeak(self, patient_number, bpm, heartbeat_reading):
        """Send data to ThingSpeak"""
        if not self.thingspeak_enabled:
            print("⚠️  ThingSpeak upload is disabled")
            return False
            
        try:
            # ThingSpeak fields:
            # field1 = Patient Number
            # field2 = BPM
            # field3 = Heartbeat Reading
            # field4 = Timestamp
            
            timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            
            payload = {
                'field1': patient_number,
                'field2': bpm,
                'field3': heartbeat_reading,
                'field4': timestamp
            }
            
            print(f"☁️  Sending to ThingSpeak: Patient {patient_number}, BPM: {bpm}, Reading: {heartbeat_reading}")
            response = requests.get(self.thingspeak_url, params=payload, timeout=10)
            
            if response.status_code == 200:
                entry_id = response.text
                if entry_id != '0':
                    print(f"✅ Data sent to ThingSpeak (Entry ID: {entry_id})")
                    print(f"📊 View at: https://thingspeak.com/channels/{self.thingspeak_channel_id}")
                    return True
                else:
                    print("❌ ThingSpeak update failed - check API key and channel permissions")
                    return False
            else:
                print(f"❌ ThingSpeak HTTP error: {response.status_code}")
                return False
                
        except requests.exceptions.RequestException as e:
            print(f"❌ ThingSpeak connection error: {e}")
            return False
        except Exception as e:
            print(f"❌ ThingSpeak error: {e}")
            return False
    
    def find_available_arduino_port(self):
        """Find Arduino port that's not busy"""
        print("🔍 Scanning for available Arduino ports...")
        
        arduino_ports = []
        for p in serial.tools.list_ports.comports():
            if any(keyword in p.device.lower() for keyword in ['usbmodem', 'usbserial']):
                arduino_ports.append(p.device)
                print(f"  📍 Found: {p.device} - {p.description}")
        
        # Try both cu and tty variants
        all_ports_to_try = []
        for port in arduino_ports:
            if 'cu.' in port:
                tty_port = port.replace('cu.', 'tty.')
                all_ports_to_try.extend([port, tty_port])
            elif 'tty.' in port:
                cu_port = port.replace('tty.', 'cu.')
                all_ports_to_try.extend([port, cu_port])
            else:
                all_ports_to_try.append(port)
        
        return list(dict.fromkeys(all_ports_to_try))
    
    def test_port(self, port):
        """Test if we can open a port"""
        try:
            print(f"  🔄 Testing {port}...")
            ser = serial.Serial(port, self.baudrate, timeout=1)
            ser.close()
            return True
        except serial.SerialException as e:
            if "Resource busy" in str(e):
                print(f"    ⚠️  {port} is busy")
            else:
                print(f"    ❌ {port} failed: {e}")
            return False
        except Exception as e:
            print(f"    ❌ {port} error: {e}")
            return False
    
    def connect(self):
        """Connect to Arduino with retry logic"""
        max_retries = 3
        retry_delay = 2
        
        for attempt in range(max_retries):
            try:
                if not self.port:
                    available_ports = self.find_available_arduino_port()
                    if not available_ports:
                        print("❌ No Arduino ports found")
                        return False
                    
                    print(f"🔄 Attempt {attempt + 1}/{max_retries}: Testing ports...")
                    working_ports = [p for p in available_ports if self.test_port(p)]
                    
                    if not working_ports:
                        print("❌ No working ports found")
                        if attempt < max_retries - 1:
                            print(f"⏳ Retrying in {retry_delay} seconds...")
                            time.sleep(retry_delay)
                        continue
                    
                    self.port = working_ports[0]
                    print(f"🎯 Selected port: {self.port}")
                
                print(f"🔌 Connecting to {self.port}...")
                self.ser = serial.Serial(self.port, self.baudrate, timeout=1)
                print(f"✅ Connected to {self.port}")
                
                time.sleep(2)
                
                if self.ser.in_waiting:
                    self.ser.reset_input_buffer()
                
                self.initialize_csv()
                self.connection_lost = False
                
                return True
                
            except serial.SerialException as e:
                if "Resource busy" in str(e):
                    print(f"❌ Port {self.port} is busy (attempt {attempt + 1}/{max_retries})")
                    if attempt < max_retries - 1:
                        print("💡 TIP: Close Arduino IDE Serial Monitor and other serial applications")
                        print(f"⏳ Retrying in {retry_delay} seconds...")
                        time.sleep(retry_delay)
                    self.port = None
                else:
                    print(f"❌ Connection failed: {e}")
                    return False
            except Exception as e:
                print(f"❌ Unexpected error: {e}")
                return False
        
        return False
    
    def check_connection(self):
        """Check if serial connection is still active"""
        if self.ser and self.ser.is_open:
            try:
                # Try to read the number of bytes in buffer
                _ = self.ser.in_waiting
                return True
            except (OSError, serial.SerialException):
                self.connection_lost = True
                return False
        return False
    
    def reconnect(self):
        """Attempt to reconnect to Arduino"""
        print("🔌 Connection lost! Attempting to reconnect...")
        self.disconnect()
        time.sleep(2)
        return self.connect()
    
    def initialize_csv(self):
        """Initialize CSV file - resume from last patient if file exists"""
        file_exists = os.path.exists(self.csv_filename)
        
        try:
            if file_exists:
                # Read the last patient number from existing CSV
                with open(self.csv_filename, 'r') as csvfile:
                    reader = csv.reader(csvfile)
                    rows = list(reader)
                    if len(rows) > 1:  # Has data beyond header
                        last_row = rows[-1]
                        self.current_patient_number = int(last_row[0])
                        print(f"📊 Resuming from Patient {self.current_patient_number}")
                    else:
                        self.current_patient_number = 0
                print(f"📁 Using existing CSV: {self.csv_filename}")
            else:
                # Create new file with headers
                with open(self.csv_filename, 'w', newline='') as csvfile:
                    writer = csv.writer(csvfile)
                    writer.writerow([
                        'Patient_Number', 'Timestamp', 'BPM', 'Heartbeat_Reading'
                    ])
                self.current_patient_number = 0
                print(f"📁 New CSV file created: {self.csv_filename}")
            
            print("📊 Recording: Patient Number, Timestamp, BPM, Heartbeat Reading")
            
            # ThingSpeak status
            if self.thingspeak_enabled:
                print("☁️  ThingSpeak integration: ENABLED")
                print(f"📊 Channel ID: {self.thingspeak_channel_id}")
            else:
                print("⚠️  ThingSpeak integration: DISABLED")
                
        except Exception as e:
            print(f"❌ Error initializing CSV file: {e}")
            # Fallback: create new file
            try:
                with open(self.csv_filename, 'w', newline='') as csvfile:
                    writer = csv.writer(csvfile)
                    writer.writerow([
                        'Patient_Number', 'Timestamp', 'BPM', 'Heartbeat_Reading'
                    ])
                self.current_patient_number = 0
                print(f"📁 New CSV file created: {self.csv_filename}")
            except Exception as e2:
                print(f"❌ Critical error creating CSV: {e2}")
    
    def save_patient_data(self):
        """Save patient data when both BPM and heartbeat are available"""
        if self.last_bpm is not None and self.last_heartbeat is not None:
            try:
                timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                
                # Save to CSV
                with open(self.csv_filename, 'a', newline='') as csvfile:
                    writer = csv.writer(csvfile)
                    writer.writerow([
                        self.current_patient_number,
                        timestamp,
                        self.last_bpm,
                        self.last_heartbeat
                    ])
                
                print(f"💾 SAVED: Patient {self.current_patient_number} | "
                      f"BPM: {self.last_bpm} | Heartbeat: {self.last_heartbeat}")
                
                # Send to ThingSpeak
                if self.thingspeak_enabled:
                    thingspeak_success = self.send_to_thingspeak(
                        self.current_patient_number, 
                        self.last_bpm, 
                        self.last_heartbeat
                    )
                    if not thingspeak_success:
                        print("⚠️  ThingSpeak upload failed, but data saved locally")
                
                # Reset for next patient
                self.last_bpm = None
                self.last_heartbeat = None
                
            except Exception as e:
                print(f"❌ Error saving patient data: {e}")
    
    def parse_data(self, line):
        """Parse only essential data for CSV recording"""
        if ':' not in line:
            return
        
        data_type, data_value = line.split(':', 1)
        
        try:
            if data_type == 'DETECT':
                # Extract patient number from detection
                if 'Queue=' in data_value:
                    queue_part = data_value.split('Queue=')[1].split(',')[0]
                    detected_patient = int(queue_part.strip())
                    
                    # If Arduino sends a lower number than our current, use ours + 1
                    if detected_patient <= self.current_patient_number:
                        self.current_patient_number += 1
                    else:
                        self.current_patient_number = detected_patient
                    
                    print(f"👤 Patient {self.current_patient_number} detected")
            
            elif data_type == 'HEARTBEAT':
                # Extract heartbeat reading
                if 'Reading=' in data_value:
                    heartbeat = data_value.replace('Reading=', '').strip()
                    self.last_heartbeat = int(heartbeat)
                    print(f"❤️  Heartbeat reading: {self.last_heartbeat}")
                else:
                    heartbeat = data_value.strip()
                    self.last_heartbeat = int(heartbeat)
                    print(f"❤️  Heartbeat reading: {self.last_heartbeat}")
            
            elif data_type == 'BPM':
                # Extract BPM value
                if 'Value=' in data_value:
                    bpm = data_value.replace('Value=', '').strip()
                    self.last_bpm = int(bpm)
                    print(f"💓 BPM: {self.last_bpm}")
                    # Save data when we have both BPM and heartbeat
                    if self.last_heartbeat is not None:
                        self.save_patient_data()
                else:
                    bpm = data_value.strip()
                    self.last_bpm = int(bpm)
                    print(f"💓 BPM: {self.last_bpm}")
                    # Save data when we have both BPM and heartbeat
                    if self.last_heartbeat is not None:
                        self.save_patient_data()
                    
        except ValueError as e:
            print(f"⚠️  Could not parse numeric value from: {line}")
        except Exception as e:
            print(f"⚠️  Error parsing data: {e}")
    
    def disconnect(self):
        """Disconnect from Arduino"""
        self.running = False
        if self.ser and self.ser.is_open:
            try:
                self.ser.close()
                print("🔌 Disconnected from Arduino")
            except:
                pass  # Ignore errors during disconnection
    
    def send_command(self, command):
        """Send command to Arduino"""
        if self.ser and self.ser.is_open:
            try:
                self.ser.write(command.encode())
                print(f"📤 Sent command: {command}")
            except Exception as e:
                print(f"❌ Failed to send command: {e}")
                self.connection_lost = True
    
    def monitor_serial(self):
        """Monitor serial data with connection recovery"""
        def serial_worker():
            reconnect_attempts = 0
            max_reconnect_attempts = 5
            
            while self.running:
                try:
                    # Check connection status
                    if not self.check_connection():
                        if not self.connection_lost:
                            print("⚠️  Connection to Arduino lost!")
                            self.connection_lost = True
                        
                        if reconnect_attempts < max_reconnect_attempts:
                            print(f"🔄 Attempting to reconnect ({reconnect_attempts + 1}/{max_reconnect_attempts})...")
                            if self.reconnect():
                                print("✅ Reconnected successfully!")
                                reconnect_attempts = 0
                                self.connection_lost = False
                                continue
                            else:
                                reconnect_attempts += 1
                                time.sleep(2)
                        else:
                            print("❌ Max reconnection attempts reached. Please check Arduino connection.")
                            time.sleep(5)
                        continue
                    
                    # Reset reconnect attempts if connection is good
                    reconnect_attempts = 0
                    
                    # Read serial data
                    if self.ser.in_waiting:
                        line = self.ser.readline().decode('utf-8').strip()
                        if line:
                            # Display everything for user visibility
                            print(f"📥 {line}")
                            
                            # Parse only essential data for CSV
                            self.parse_data(line)
                            
                except UnicodeDecodeError:
                    print("⚠️  Could not decode serial data")
                except Exception as e:
                    if "Device not configured" in str(e) or "disconnected" in str(e).lower():
                        self.connection_lost = True
                        print("⚠️  Arduino disconnected!")
                    else:
                        print(f"❌ Serial read error: {e}")
                
                time.sleep(0.1)
        
        self.running = True
        thread = threading.Thread(target=serial_worker)
        thread.daemon = True
        thread.start()
        print("📡 Started serial monitoring...")
        print("💡 Recording: Patient Number, Timestamp, BPM, Heartbeat Reading")
        print(f"📁 CSV file: {self.csv_filename}")
        if self.thingspeak_enabled:
            print("☁️  ThingSpeak: ENABLED - Data will be uploaded to cloud")
            print(f"🌐 View live data: https://thingspeak.com/channels/{self.thingspeak_channel_id}")
        print("🛡️  Auto-reconnect enabled - will attempt to reconnect if Arduino is disconnected")
    
    def start_interactive(self):
        """Start interactive monitoring session"""
        if not self.connect():
            print("\n🔧 TROUBLESHOOTING:")
            print("1. ✅ Make sure Arduino is connected via USB")
            print("2. ✅ Close Arduino IDE Serial Monitor completely")
            print("3. ✅ Close any other serial terminal applications")
            print("4. 🔄 Try unplugging and replugging the Arduino")
            return
        
        self.monitor_serial()
        
        print("\n🎮 INTERACTIVE MODE - Commands:")
        print("  R = Reset patient counter")
        print("  Q = Quit program")
        print("  RECONNECT = Force reconnection")
        print("  STATUS = Check connection status")
        print("  THINGSPEAK = Toggle ThingSpeak upload")
        print("  Note: Only patient number, BPM and heartbeat are recorded in CSV")
        
        try:
            while self.running:
                try:
                    cmd = input().strip().upper()
                    
                    if cmd == 'Q':
                        print("👋 Exiting program...")
                        break
                    elif cmd == 'R':
                        self.current_patient_number = 0
                        self.send_command('R')
                        print("🔄 Patient counter reset to 0")
                    elif cmd == 'RECONNECT':
                        if self.reconnect():
                            print("✅ Manual reconnection successful!")
                        else:
                            print("❌ Manual reconnection failed")
                    elif cmd == 'STATUS':
                        if self.check_connection():
                            print("✅ Connected to Arduino")
                        else:
                            print("❌ Disconnected from Arduino")
                        print(f"☁️  ThingSpeak: {'ENABLED' if self.thingspeak_enabled else 'DISABLED'}")
                    elif cmd == 'THINGSPEAK':
                        self.thingspeak_enabled = not self.thingspeak_enabled
                        status = "ENABLED" if self.thingspeak_enabled else "DISABLED"
                        print(f"☁️  ThingSpeak upload {status}")
                    elif cmd == 'HELP':
                        print("📋 Commands: R, Q, RECONNECT, STATUS, THINGSPEAK, HELP")
                    else:
                        print("❓ Unknown command. Type HELP for options")
                        
                except EOFError:
                    break
                except KeyboardInterrupt:
                    print("\n🛑 Stopped by user")
                    break
                
        except KeyboardInterrupt:
            print("\n🛑 Stopped by user")
        
        finally:
            self.disconnect()

def main():
    port = sys.argv[1] if len(sys.argv) > 1 else None
    
    if port:
        print(f"🎯 Using specified port: {port}")
    else:
        print("🔍 Auto-detecting Arduino port...")
    
    monitor = HealthMonitor(port=port)
    monitor.start_interactive()

if __name__ == "__main__":
    main()