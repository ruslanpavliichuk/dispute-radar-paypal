import { themeQuartz } from "ag-grid-community";

// One look for the grids, the charts and Studio, so the three read as one product.
export const brand = { blue: "#0b5fff", red: "#d92d3a", green: "#1f8f55", amber: "#d98a00", ink: "#14213d", paper: "#f6f8fc" };

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
  palette: { fills: [brand.blue, brand.red, brand.green, brand.amber], strokes: [brand.blue, brand.red, brand.green, brand.amber] },
  overrides: { common: { title: { fontSize: 15, fontWeight: 600, color: brand.ink } } },
};

export const STATUS_COLOURS: Record<string, string> = {
  PROPOSED: brand.amber, SENDING: brand.blue, SENT: brand.green, FAILED: brand.red, REJECTED: "#6b7280",
};
