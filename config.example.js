// Instellingen voor de kaart. Alles is optioneel.
window.OV_CONFIG = {
  // Adres van live_proxy.py, bijvoorbeeld "http://raspberrypi.local:8787/departures".
  // Leeg laten = tabblad Vertrektijden toont de uitleg.
  liveEndpoint: "",
  // Live voertuigen komen van hetzelfde tussenstation (/vehicles); dat adres wordt afgeleid.
  // Aantal seconden tussen twee verversingen.
  liveInterval: 30
  // tiles: "https://tile.openstreetmap.org/{z}/{x}/{y}.png"   // andere achtergrondkaart
};
