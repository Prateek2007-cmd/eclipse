/** Design-token colours, mirrored from tokens.css.
 *
 * Three.js cannot consume CSS variables, so the palette lives
 * here too — as the single TS source. Do not hardcode hex in
 * scene components; import from this module. (Review N4.) */
export const COLORS = {
  bg: "#070b12",
  bgRaised: "#0b111c",
  surface: "#12212b",
  border: "#1e3a4a",
  accent: "#ff6b35",
  text: "#e8f0f2",
  success: "#34d399",
  warning: "#fbbf24",
  danger: "#f87171",
  info: "#60a5fa",
} as const;
