
#include <Arduino.h>
#include <WiFi.h>
#include <WiFiClient.h>
#include <HTTPClient.h>
#include <PubSubClient.h>
#include <DHT.h>
#include <ArduinoJson.h>

#include "secrets.h"


// --------------------------------------------------
// PIN CONFIGURATION
// --------------------------------------------------

#define DHT_PIN 15
#define DHT_TYPE DHT22

#define LED_PIN 2


// --------------------------------------------------
// MQTT CONFIGURATION
// --------------------------------------------------

const char* MQTT_BROKER = "135.232.203.144";
const int MQTT_PORT = 1883;


// MQTT topics
String settingsTopic;
String stateTopic;
String statusTopic;


// --------------------------------------------------
// GLOBAL OBJECTS
// --------------------------------------------------

DHT dht(DHT_PIN, DHT_TYPE);

WiFiClient wifiClient;
PubSubClient mqttClient(wifiClient);


// --------------------------------------------------
// GLOBAL VARIABLES
// --------------------------------------------------

String g_mac;

float currentHumidity = 0.0;

float maxHumidity = 65.0;

String fanMode = "off";

bool settingsReceived = false;

bool fanOn = false;

bool humidityAvailable = false;

bool stateNeedsPublishing = false;


// Timers
unsigned long lastReading = 0;
unsigned long lastMqttAttempt = 0;
unsigned long lastWifiAttempt = 0;

const unsigned long READING_INTERVAL = 10000;
const unsigned long RECONNECT_INTERVAL = 5000;


// --------------------------------------------------
// FAN CONTROL
// --------------------------------------------------

void setFanState(bool newState) {

    // Only update when the fan state changes.
    if (fanOn == newState) {
        return;
    }

    fanOn = newState;

    digitalWrite(
        LED_PIN,
        fanOn ? HIGH : LOW
    );

    stateNeedsPublishing = true;

    Serial.print("Fan state changed: ");

    Serial.println(
        fanOn ? "ON" : "OFF"
    );

    // Publish immediately if MQTT is connected.
    if (mqttClient.connected()) {

        const char* state = fanOn ? "on" : "off";

        if (mqttClient.publish(
                stateTopic.c_str(),
                state,
                true
            )) {

            stateNeedsPublishing = false;

            Serial.println("Fan state published.");

        }
    }
}


// --------------------------------------------------
// APPLY FAN SETTINGS
// --------------------------------------------------

void applyFanLogic() {

    if (!settingsReceived) {

        setFanState(false);

        return;
    }

    if (fanMode == "on") {

        setFanState(true);

    }

    else if (fanMode == "off") {

        setFanState(false);

    }

    else if (fanMode == "auto") {

        if (!humidityAvailable) {
            return;
        }

        if (currentHumidity > maxHumidity) {

            setFanState(true);

        }

        else {

            setFanState(false);

        }
    }
}


// --------------------------------------------------
// MQTT MESSAGE CALLBACK
// --------------------------------------------------

void onMqttMessage(
    char* topic,
    byte* payload,
    unsigned int length
) {

    Serial.println("\nMQTT message received.");

    // Make sure the message is for fan settings.
    if (String(topic) != settingsTopic) {
        return;
    }

    // Convert payload into JSON.
    JsonDocument doc;

    DeserializationError error =
        deserializeJson(doc, payload, length);

    // Reject invalid JSON.
    if (error) {

        Serial.println("Invalid JSON. Settings ignored.");

        return;
    }

    // Both fields must exist and have correct types.
    if (
        !doc["mode"].is<const char*>() ||
        (
            !doc["max_humidity"].is<float>() &&
            !doc["max_humidity"].is<int>()
        )
    ) {

        Serial.println("Missing or invalid fields.");

        return;
    }

    String newMode = doc["mode"].as<String>();

    float newMaxHumidity =
        doc["max_humidity"].as<float>();

    // Validate mode.
    if (
        newMode != "auto" &&
        newMode != "on" &&
        newMode != "off"
    ) {

        Serial.println("Invalid fan mode. Settings ignored.");

        return;
    }

    // Validate humidity limit.
    if (
        newMaxHumidity < 0 ||
        newMaxHumidity > 100
    ) {

        Serial.println("Invalid humidity limit.");

        return;
    }

    // Only update after ALL validation passes.
    fanMode = newMode;

    maxHumidity = newMaxHumidity;

    settingsReceived = true;

    Serial.println("New settings accepted.");

    Serial.print("Mode: ");
    Serial.println(fanMode);

    Serial.print("Maximum humidity: ");
    Serial.println(maxHumidity);

    // React immediately without waiting for next reading.
    applyFanLogic();
}


// --------------------------------------------------
// WIFI CONNECTION
// --------------------------------------------------

void connectWiFi() {

    if (WiFi.status() == WL_CONNECTED) {
        return;
    }

    Serial.println("Connecting to WiFi...");

    WiFi.begin(
        WIFI_SSID,
        WIFI_PASSWORD,
        6
    );
}


// --------------------------------------------------
// MQTT CONNECTION
// --------------------------------------------------

void connectMQTT() {

    if (
        WiFi.status() != WL_CONNECTED ||
        mqttClient.connected()
    ) {
        return;
    }

    unsigned long now = millis();

    if (
        now - lastMqttAttempt <
        RECONNECT_INTERVAL
    ) {
        return;
    }

    lastMqttAttempt = now;

    Serial.println("Connecting to MQTT broker...");

    // Unique client ID using device MAC.
    String clientId = "fan-" + g_mac;

    clientId.replace(":", "");

    // Configure retained last will.
    bool connected = mqttClient.connect(

        clientId.c_str(),

        nullptr,
        nullptr,

        statusTopic.c_str(),

        1,
        true,

        "offline"
    );

    if (connected) {

        Serial.println("MQTT connected.");

        // Publish online status.
        mqttClient.publish(
            statusTopic.c_str(),
            "online",
            true
        );

        // Subscribe to settings.
        mqttClient.subscribe(
            settingsTopic.c_str()
        );

        Serial.println("Subscribed to fan/settings.");

        // Publish latest fan state if a change
        // occurred while disconnected.
        if (stateNeedsPublishing) {

            const char* state =
                fanOn ? "on" : "off";

            if (mqttClient.publish(
                    stateTopic.c_str(),
                    state,
                    true
                )) {

                stateNeedsPublishing = false;
            }
        }

    }

    else {

        Serial.print("MQTT connection failed. State: ");

        Serial.println(mqttClient.state());

    }
}


// --------------------------------------------------
// POST HUMIDITY READING TO API
// --------------------------------------------------

void postReading(float humidity) {

    if (WiFi.status() != WL_CONNECTED) {

        Serial.println("WiFi unavailable. Reading not posted.");

        return;
    }

    if (String(DEVICE_KEY).length() == 0) {

        Serial.println("DEVICE_KEY is empty.");

        Serial.println("Register this fan with POST /fans first.");

        return;
    }

    HTTPClient http;

    WiFiClient client;

    String url = String(API_URL) + "/readings";

    http.begin(client, url);

    http.addHeader(
        "Content-Type",
        "application/json"
    );

    http.addHeader(
        "X-API-Key",
        DEVICE_KEY
    );

    // Construct JSON body.
    JsonDocument doc;

    doc["mac"] = g_mac;

    doc["humidity"] = humidity;

    String requestBody;

    serializeJson(doc, requestBody);

    Serial.println("\nSending humidity reading...");

    Serial.println(requestBody);

    int responseCode = http.POST(requestBody);

    if (responseCode > 0) {

        Serial.print("HTTP response: ");

        Serial.println(responseCode);

        Serial.println(http.getString());

    }

    else {

        Serial.print("HTTP request failed: ");

        Serial.println(http.errorToString(responseCode));

    }

    http.end();
}


// --------------------------------------------------
// READ DHT22
// --------------------------------------------------

void readHumidity() {

    float humidity = dht.readHumidity();

    if (isnan(humidity)) {

        Serial.println("Failed to read DHT22.");

        return;
    }

    currentHumidity = humidity;

    humidityAvailable = true;

    Serial.print("Humidity: ");

    Serial.print(currentHumidity);

    Serial.println("%");

    // Update fan immediately using latest humidity.
    applyFanLogic();

    // Send reading to API.
    postReading(currentHumidity);
}


// --------------------------------------------------
// SETUP
// --------------------------------------------------

void setup() {

    Serial.begin(115200);

    Serial.println("\n\nECSE3038 Bathroom Fan");

    // Configure LED.
    pinMode(LED_PIN, OUTPUT);

    digitalWrite(LED_PIN, LOW);

    // Fan initially OFF.
    fanOn = false;

    // Initialize DHT22.
    dht.begin();

    // Build MAC address.
    g_mac = WiFi.macAddress();

    g_mac.toLowerCase();

    Serial.print("Device MAC: ");

    Serial.println(g_mac);

    // Construct MQTT topics.
    String baseTopic =
        "ecse3038/" + String(MQTT_NAME);

    settingsTopic =
        baseTopic + "/fan/settings";

    stateTopic =
        baseTopic + "/fan/state";

    statusTopic =
        baseTopic + "/status";

    // Configure MQTT.
    mqttClient.setServer(
        MQTT_BROKER,
        MQTT_PORT
    );

    mqttClient.setCallback(onMqttMessage);

    mqttClient.setBufferSize(512);

    mqttClient.setKeepAlive(60);

    // Connect to WiFi.
    connectWiFi();

    // Allow first MQTT connection attempt immediately.
    lastMqttAttempt = millis() - RECONNECT_INTERVAL;

    Serial.println("Setup complete.");

    Serial.println("Waiting for fan settings...");
}


// --------------------------------------------------
// MAIN LOOP
// --------------------------------------------------

void loop() {

    // Maintain WiFi connection.
    if (WiFi.status() != WL_CONNECTED) {

        unsigned long now = millis();

        if (
            now - lastWifiAttempt >=
            RECONNECT_INTERVAL
        ) {

            lastWifiAttempt = now;

            connectWiFi();
        }

        delay(10);

        return;
    }

    // Maintain MQTT connection.
    if (!mqttClient.connected()) {

        connectMQTT();
    }

    // Process incoming MQTT messages.
    mqttClient.loop();

    // Read humidity every 10 seconds.
    unsigned long now = millis();

    if (
        now - lastReading >=
        READING_INTERVAL
    ) {

        lastReading = now;

        readHumidity();
    }
}
