const startDeviceButton =
  document.getElementById("startDeviceButton") || document.getElementById("startButton");
const runTestButton =
  document.getElementById("runTestButton") || document.getElementById("runButton");
const stopDeviceButton =
  document.getElementById("stopDeviceButton") || document.getElementById("stopButton");
const wsUrlInput = document.getElementById("wsUrl");
const statusEl = document.getElementById("status");
const sessionStateEl = document.getElementById("sessionState");
const testStateEl = document.getElementById("testState");
const temperatureValueEl = document.getElementById("temperatureValue");
const imageEl = document.getElementById("streamImage");
const placeholderEl = document.getElementById("placeholder");

let socket = null;
let currentObjectUrl = null;
let pendingAction = null;
let deviceActive = false;
let testRunning = false;
let heaterEnabled = false;
let isConnecting = false;
let isStopping = false;

function hasRequiredUi() {
  return Boolean(
    startDeviceButton &&
      runTestButton &&
      stopDeviceButton &&
      wsUrlInput &&
      statusEl &&
      sessionStateEl &&
      testStateEl &&
      temperatureValueEl &&
      imageEl &&
      placeholderEl,
  );
}

function logClient(message, detail) {
  if (detail === undefined) {
    console.info(`[test_webapp] ${message}`);
    return;
  }
  console.info(`[test_webapp] ${message}`, detail);
}

if (!hasRequiredUi()) {
  console.error("[test_webapp] Required UI elements are missing. Reload the page to fetch the latest test_webapp files.");
} else {

function defaultWebSocketUrl() {
  const pageProtocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const host = window.location.hostname || "localhost";
  return `${pageProtocol}//${host}:8765/stream`;
}

function normalizeWebSocketUrl(rawValue) {
  const trimmed = rawValue.trim();
  if (!trimmed) {
    return defaultWebSocketUrl();
  }

  if (trimmed.startsWith("ws://") || trimmed.startsWith("wss://")) {
    return trimmed;
  }

  if (trimmed.startsWith("http://") || trimmed.startsWith("https://")) {
    const parsed = new URL(trimmed);
    parsed.protocol = parsed.protocol === "https:" ? "wss:" : "ws:";
    if (!parsed.pathname || parsed.pathname === "/") {
      parsed.pathname = "/stream";
    }
    return parsed.toString();
  }

  const inferred = new URL(`${window.location.protocol}//${trimmed}`);
  inferred.protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  if (!inferred.pathname || inferred.pathname === "/") {
    inferred.pathname = "/stream";
  }
  return inferred.toString();
}

function setStatus(message, state) {
  statusEl.textContent = message;
  statusEl.dataset.state = state;
}

function setSessionState(message) {
  sessionStateEl.textContent = message;
}

function setTestState(message) {
  testStateEl.textContent = message;
}

function setTemperatureValue(value) {
  temperatureValueEl.textContent = value;
}

function cleanupImageUrl() {
  if (currentObjectUrl) {
    URL.revokeObjectURL(currentObjectUrl);
    currentObjectUrl = null;
  }
}

function clearStreamView() {
  cleanupImageUrl();
  imageEl.removeAttribute("src");
  placeholderEl.hidden = false;
}

function updateButtonState() {
  startDeviceButton.disabled = deviceActive || isConnecting || isStopping;
  runTestButton.disabled = !deviceActive || testRunning || isConnecting || isStopping;
  stopDeviceButton.disabled = !deviceActive || isConnecting || isStopping;
}

function resetUiToIdle(statusMessage = "Idle", statusState = "pending") {
  deviceActive = false;
  testRunning = false;
  heaterEnabled = false;
  isConnecting = false;
  isStopping = false;
  clearStreamView();
  setTemperatureValue("--.- C");
  setSessionState("Idle");
  setTestState("Idle");
  setStatus(statusMessage, statusState);
  updateButtonState();
}

function closeSocketIfNeeded() {
  if (socket) {
    socket.close();
    socket = null;
  }
}

function sendAction(action) {
  if (!socket || socket.readyState !== WebSocket.OPEN) {
    setStatus("WebSocket is not connected.", "error");
    return;
  }
  socket.send(JSON.stringify({ action }));
}

function connectSocketIfNeeded(actionOnOpen) {
  if (socket && socket.readyState === WebSocket.OPEN) {
    pendingAction = null;
    logClient("Socket already open; reusing existing connection.");
    return true;
  }

  if (socket && socket.readyState === WebSocket.CONNECTING) {
    pendingAction = actionOnOpen;
    isConnecting = true;
    setStatus("Connecting...", "pending");
    updateButtonState();
    return false;
  }

  let wsUrl;
  try {
    wsUrl = normalizeWebSocketUrl(wsUrlInput.value);
  } catch (_error) {
    setStatus("Enter a valid WebSocket server URL.", "error");
    return false;
  }

  wsUrlInput.value = wsUrl;
  pendingAction = actionOnOpen;
  isConnecting = true;
  logClient("Opening WebSocket connection", wsUrl);
  socket = new WebSocket(wsUrl);
  socket.binaryType = "blob";
  attachSocketListeners(socket);
  setStatus("Connecting...", "pending");
  setSessionState("Connecting");
  updateButtonState();
  return false;
}

function attachSocketListeners(activeSocket) {
  activeSocket.addEventListener("open", () => {
    isConnecting = false;
    logClient("WebSocket connected.");
    setStatus("Connected.", "ok");
    updateButtonState();
    if (pendingAction) {
      const action = pendingAction;
      pendingAction = null;
      sendAction(action);
    }
  });

  activeSocket.addEventListener("message", (event) => {
    if (typeof event.data === "string") {
      logClient("Received server message", event.data);
      handleJsonMessage(event.data);
      return;
    }

    logClient("Received video frame.");
    cleanupImageUrl();
    currentObjectUrl = URL.createObjectURL(event.data);
    imageEl.src = currentObjectUrl;
    placeholderEl.hidden = false;
    placeholderEl.hidden = true;
    if (testRunning) {
      setStatus("Video stream active.", "ok");
      setTestState("Running");
    }
  });

  activeSocket.addEventListener("close", () => {
    logClient("WebSocket closed.");
    socket = null;
    pendingAction = null;
    isConnecting = false;
    isStopping = false;
    if (deviceActive) {
      resetUiToIdle("Disconnected.", "error");
      setSessionState("Disconnected");
      setTestState("Stopped");
      return;
    }
    resetUiToIdle("Idle", "pending");
  });

  activeSocket.addEventListener("error", () => {
    logClient("WebSocket error.");
    isConnecting = false;
    isStopping = false;
    setStatus("WebSocket connection failed.", "error");
    updateButtonState();
  });
}

function handleJsonMessage(rawPayload) {
  let payload;
  try {
    payload = JSON.parse(rawPayload);
  } catch (_error) {
    return;
  }

  switch (payload.type) {
    case "info":
      return;
    case "device_started":
      deviceActive = true;
      testRunning = false;
      isStopping = false;
      setStatus(`Device ready. Temperature telemetry active. Target test temperature ${payload.targetCelsius} C.`, "ok");
      setSessionState("Ready / Telemetry Active");
      setTestState("Idle");
      updateButtonState();
      return;
    case "device_already_active":
      deviceActive = true;
      isStopping = false;
      setStatus(payload.message || "Device is already active.", "pending");
      setSessionState("Ready / Telemetry Active");
      setTestState(testRunning ? "Running" : "Idle");
      updateButtonState();
      return;
    case "temperature":
      if (typeof payload.celsius === "number") {
        setTemperatureValue(`${payload.celsius.toFixed(1)} C`);
      } else {
        setTemperatureValue("Sensor unavailable");
      }
      return;
    case "heater_state":
      heaterEnabled = Boolean(payload.enabled);
      if (deviceActive) {
        if (testRunning) {
          setSessionState(heaterEnabled ? "Testing / Heater ON" : "Testing / Heater Cycling");
        } else {
          setSessionState("Ready / Telemetry Active");
        }
      }
      return;
    case "target_reached":
      setStatus(
        `Target reached at ${payload.celsius.toFixed(1)} C. Holding near ${payload.targetCelsius} C.`,
        "ok",
      );
      setSessionState("Testing / At Temperature");
      return;
    case "test_started":
      testRunning = true;
      isStopping = false;
      clearStreamView();
      setStatus(`Test running for ${payload.durationSeconds} seconds.`, "ok");
      setTestState("Running");
      updateButtonState();
      return;
    case "test_already_running":
      testRunning = true;
      isStopping = false;
      setStatus(payload.message || "Test is already running.", "pending");
      setTestState("Running");
      updateButtonState();
      return;
    case "test_completed":
      testRunning = false;
      clearStreamView();
      setStatus("Test completed. Analysis handoff started.", "ok");
      setTestState("Completed");
      if (deviceActive) {
        setSessionState("Ready / Telemetry Active");
      }
      updateButtonState();
      return;
    case "test_stopped":
      testRunning = false;
      clearStreamView();
      setStatus(`Test stopped: ${payload.reason}.`, "pending");
      setTestState(`Stopped: ${payload.reason}`);
      if (deviceActive) {
        setSessionState("Ready / Telemetry Active");
      }
      updateButtonState();
      return;
    case "device_error":
    case "session_error":
      isStopping = false;
      setStatus(payload.message || "Device error.", "error");
      setSessionState("Error");
      updateButtonState();
      return;
    case "session_busy":
      isConnecting = false;
      setStatus(payload.message || "Another device session is already active.", "error");
      setSessionState("Busy");
      updateButtonState();
      return;
    case "invalid_action":
      isStopping = false;
      setStatus(payload.message || "Action rejected.", "error");
      updateButtonState();
      return;
    case "device_stopped":
      resetUiToIdle(`Device stopped: ${payload.reason}.`, "pending");
      setSessionState(`Stopped: ${payload.reason}`);
      return;
    default:
      return;
  }
}

wsUrlInput.value = defaultWebSocketUrl();
resetUiToIdle();
logClient("Test webapp initialized.");

startDeviceButton.addEventListener("click", () => {
  logClient("Start Device clicked.");
  if (!connectSocketIfNeeded("start_device")) {
    if (socket && socket.readyState !== WebSocket.OPEN) {
      return;
    }
  }
  sendAction("start_device");
});

runTestButton.addEventListener("click", () => {
  logClient("Run Test clicked.");
  if (!deviceActive) {
    setStatus("Start Device before running a test.", "error");
    return;
  }
  if (!socket || socket.readyState !== WebSocket.OPEN) {
    setStatus("Device connection is not active.", "error");
    return;
  }
  sendAction("run_test");
});

stopDeviceButton.addEventListener("click", () => {
  logClient("Stop Device clicked.");
  if (!socket || socket.readyState !== WebSocket.OPEN) {
    resetUiToIdle("Device already stopped.", "pending");
    return;
  }
  isStopping = true;
  setStatus("Stopping device...", "pending");
  setSessionState("Stopping");
  updateButtonState();
  sendAction("stop_device");
});

window.addEventListener("beforeunload", () => {
  clearStreamView();
  if (socket && socket.readyState === WebSocket.OPEN) {
    try {
      socket.send(JSON.stringify({ action: "stop_device" }));
    } catch (_error) {
      // Best-effort cleanup only.
    }
  }
  closeSocketIfNeeded();
});
}
