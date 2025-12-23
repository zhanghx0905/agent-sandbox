#!/bin/bash

HOST="0.0.0.0"
PID_FILE="main.pid"

start_app() {
    echo "Starting FastAPI app..."
    nohup uvicorn main:app --host $HOST --port 8000 > main.log 2>&1 &
    echo $! > $PID_FILE
    echo "App started with PID $(cat $PID_FILE)"
}

stop_app() {
    if [ -f "$PID_FILE" ]; then
        PID=$(cat $PID_FILE)
        echo "Stopping app with PID $PID..."
        kill $PID
        rm -f $PID_FILE
        echo "App stopped."
    else
        echo "PID file not found. Is the app running?"
    fi
}

status_app() {
    if [ -f "$PID_FILE" ]; then
        PID=$(cat $PID_FILE)
        if ps -p $PID > /dev/null; then
            echo "App is running with PID $PID"
        else
            echo "App PID file exists but process not running"
        fi
    else
        echo "App is not running"
    fi
}

case "$1" in
    start)
        start_app
        ;;
    stop)
        stop_app
        ;;
    status)
        status_app
        ;;
    restart)
        stop_app
        sleep 2
        start_app
        ;;
    *)
        echo "Usage: $0 {start|stop|restart|status}"
        exit 1
esac
