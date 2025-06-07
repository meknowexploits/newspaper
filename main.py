from flask import Flask, jsonify, request, send_from_directory, render_template
from flask_socketio import SocketIO, emit, join_room, leave_room
import os
import json
import threading
import time
from datetime import datetime

app = Flask(__name__)
app.config['SECRET_KEY'] = 'newspaper-delivery-sync-key-2024'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# In-memory storage for house states per city
# Format: {city_name: {house_id: {subscribed: bool, delivered: bool, last_updated: timestamp}}}
city_states = {}

# Connected users tracking
# Format: {session_id: {city: city_name, joined_at: timestamp}}
connected_users = {}

# File-based persistence
DATA_DIR = "newspaper_data"
os.makedirs(DATA_DIR, exist_ok=True)

def load_city_states(city):
    """Load states from file for a specific city"""
    filename = os.path.join(DATA_DIR, f"{city.lower()}.json")
    try:
        if os.path.exists(filename):
            with open(filename, 'r') as f:
                return json.load(f)
    except Exception as e:
        print(f"Error loading states for {city}: {e}")
    return {}

def save_city_states(city, states):
    """Save states to file for a specific city"""
    filename = os.path.join(DATA_DIR, f"{city.lower()}.json")
    try:
        # Add metadata
        save_data = {
            "states": states,
            "last_updated": datetime.now().isoformat(),
            "city": city
        }
        with open(filename, 'w') as f:
            json.dump(save_data, f, indent=2)
        print(f"Saved states for {city}: {len(states)} houses")
    except Exception as e:
        print(f"Error saving states for {city}: {e}")

def get_city_states(city):
    """Get states for a city, loading from file if not in memory"""
    city_key = city.lower()
    if city_key not in city_states:
        loaded_data = load_city_states(city)
        if isinstance(loaded_data, dict) and "states" in loaded_data:
            city_states[city_key] = loaded_data["states"]
        else:
            city_states[city_key] = loaded_data or {}
    return city_states[city_key]

@app.route("/")
def index():
    """Serve the main HTML file"""
    return render_template("index.html")

@app.route("/health")
def health():
    """Health check endpoint"""
    return jsonify({
        "status": "healthy",
        "connected_users": len(connected_users),
        "active_cities": len(city_states),
        "timestamp": datetime.now().isoformat()
    })

@app.route("/stats")
def stats():
    """Get server statistics"""
    stats_data = {}
    for city, states in city_states.items():
        total_houses = len(states)
        subscribed = sum(1 for state in states.values() if state.get("subscribed", True))
        delivered = sum(1 for state in states.values() if state.get("delivered", False) and state.get("subscribed", True))

        stats_data[city] = {
            "total_houses": total_houses,
            "subscribed": subscribed,
            "delivered": delivered,
            "pending": subscribed - delivered
        }

    return jsonify({
        "cities": stats_data,
        "connected_users": len(connected_users),
        "timestamp": datetime.now().isoformat()
    })

@socketio.on('connect')
def handle_connect():
    """Handle client connection"""
    session_id = request.sid
    connected_users[session_id] = {
        'city': None,
        'joined_at': datetime.now().isoformat()
    }

    print(f'📱 Client connected: {session_id} (Total: {len(connected_users)})')
    emit('connection_status', {
        'status': 'connected',
        'message': '✅ Connected to sync server',
        'server_time': datetime.now().isoformat()
    })

@socketio.on('disconnect')
def handle_disconnect():
    """Handle client disconnection"""
    session_id = request.sid
    if session_id in connected_users:
        user_info = connected_users[session_id]
        city = user_info.get('city')

        if city:
            leave_room(f'city_{city.lower()}')
            print(f'📤 Client {session_id} left city: {city}')

        del connected_users[session_id]
        print(f'📱 Client disconnected: {session_id} (Remaining: {len(connected_users)})')

@socketio.on('join_city')
def handle_join_city(data):
    """Handle client joining a city room"""
    city = data.get('city', '').lower()
    session_id = request.sid

    if not city:
        emit('error', {'message': 'City name required'})
        return

    # Leave previous city room if any
    if session_id in connected_users and connected_users[session_id]['city']:
        old_city = connected_users[session_id]['city']
        leave_room(f'city_{old_city}')
        print(f'📤 Client {session_id} left city: {old_city}')

    # Join new city room
    join_room(f'city_{city}')
    connected_users[session_id]['city'] = city

    # Get current states for the city
    current_states = get_city_states(city)

    # Send current states to the joining user
    emit('city_states_sync', {
        'city': city,
        'states': current_states,
        'timestamp': datetime.now().isoformat()
    })

    print(f'📥 Client {session_id} joined city: {city} ({len(current_states)} houses)')

@socketio.on('house_state_change')
def handle_house_state_change(data):
    """Handle house state changes and broadcast to other users"""
    session_id = request.sid

    if session_id not in connected_users:
        emit('error', {'message': 'User not registered'})
        return

    city = data.get('city', '').lower()
    house_id = str(data.get('house_id', ''))
    new_state = data.get('state', {})

    if not city or not house_id or not isinstance(new_state, dict):
        emit('error', {'message': 'Invalid data format'})
        return

    # Validate state data
    if 'subscribed' not in new_state and 'delivered' not in new_state:
        emit('error', {'message': 'State must contain subscribed or delivered status'})
        return

    # Get current states for the city
    current_states = get_city_states(city)

    # Update the house state
    if house_id not in current_states:
        current_states[house_id] = {}

    # Merge new state with existing state
    current_states[house_id].update(new_state)
    current_states[house_id]['last_updated'] = datetime.now().isoformat()
    current_states[house_id]['updated_by'] = session_id

    # Save to file
    save_city_states(city, current_states)

    # Broadcast to all other users in the same city
    emit('house_state_sync', {
        'city': city,
        'house_id': house_id,
        'state': current_states[house_id],
        'timestamp': datetime.now().isoformat()
    }, room=f'city_{city}', include_self=False)

    print(f'🏠 State updated for {city}: House {house_id} = {new_state} by {session_id}')

@socketio.on('get_city_states')
def handle_get_city_states(data):
    """Handle request for current city states"""
    city = data.get('city', '').lower()

    if not city:
        emit('error', {'message': 'City name required'})
        return

    current_states = get_city_states(city)

    emit('city_states_sync', {
        'city': city,
        'states': current_states,
        'timestamp': datetime.now().isoformat()
    })

@socketio.on('ping')
def handle_ping():
    """Handle ping for connection testing"""
    emit('pong', {'timestamp': datetime.now().isoformat()})

# Background task to save states periodically
def periodic_save():
    """Periodically save all city states to ensure data persistence"""
    while True:
        try:
            time.sleep(300)  # Save every 5 minutes
            for city, states in city_states.items():
                if states:  # Only save if there are states
                    save_city_states(city, states)
            print(f"🔄 Periodic save completed for {len(city_states)} cities")
        except Exception as e:
            print(f"❌ Error in periodic save: {e}")

# Background task for connection health monitoring
def connection_monitor():
    """Monitor connections and emit periodic updates"""
    while True:
        try:
            time.sleep(60)  # Check every minute
            if connected_users:
                # Emit heartbeat to all connected users
                socketio.emit('heartbeat', {
                    'timestamp': datetime.now().isoformat(),
                    'connected_users': len(connected_users)
                })
            print(f"💓 Heartbeat sent to {len(connected_users)} users")
        except Exception as e:
            print(f"❌ Error in connection monitor: {e}")

if __name__ == "__main__":
    print("🚀 Starting Newspaper Delivery Sync Server...")
    print(f"📁 Data directory: {DATA_DIR}")

    # Start background threads
    save_thread = threading.Thread(target=periodic_save, daemon=True)
    save_thread.start()

    monitor_thread = threading.Thread(target=connection_monitor, daemon=True)
    monitor_thread.start()

    print("✅ Background tasks started")
    print("🌐 Server starting on http://0.0.0.0:5000")

    # Run the server
    socketio.run(
        app,
        debug=True,
        host='0.0.0.0',
        port=5000,
        allow_unsafe_werkzeug=True
    )