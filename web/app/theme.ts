import { themeQuartz } from "ag-grid-community";

// One look for the grids, the charts and Studio, so the three read as one product.
// PayPal-inspired palette (navy, blue, sky, gold); no PayPal logo or wordmark is used
export const brand = { blue: "#0070E0", navy: "#003087", sky: "#009CDE", gold: "#FFC439", red: "#D92D3A", green: "#1F8F55", amber: "#D98A00", ink: "#001C64", paper: "#F5F7FA" };

export const gridTheme = themeQuartz.withParams({
  accentColor: brand.blue,
  foregroundColor: brand.ink,
  headerBackgroundColor: brand.paper,
  headerFontWeight: 600,
  oddRowBackgroundColor: "#fbfcfe",
  borderRadius: 8,
  wrapperBorderRadius: 12,
  spacing: 7,
  fontSize: 13,
});

export const chartTheme = {
  baseTheme: "ag-default" as const,
  palette: { fills: [brand.blue, brand.navy, brand.sky, brand.gold], strokes: [brand.blue, brand.navy, brand.sky, brand.gold] },
  overrides: { common: { title: { fontSize: 15, fontWeight: 600, color: brand.ink } } },
};

export const STATUS_COLOURS: Record<string, string> = {
  PROPOSED: brand.amber, SENDING: brand.blue, SENT: brand.green, FAILED: brand.red, REJECTED: "#6b7280",
};
