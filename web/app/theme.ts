import { themeQuartz } from "ag-grid-community";

// One look for the grids, the charts and Studio, so the three read as one product.
// PayPal-inspired palette (navy, blue, sky, gold); no PayPal logo or wordmark is used
export const brand = { blue: "#0070E0", navy: "#003087", sky: "#009CDE", gold: "#FFC439", red: "#D92D3A", green: "#1F8F55", amber: "#D98A00", ink: "#001C64", paper: "#F5F7FA" };

export const gridTheme = themeQuartz.withParams({
  accentColor: brand.blue,
  foregroundColor: brand.ink,
  backgroundColor: "#ffffff",
  headerBackgroundColor: "#ffffff",
  headerTextColor: "#5C6B8A",
  headerFontWeight: 500,
  headerFontSize: 12,
  oddRowBackgroundColor: "#ffffff",
  rowHoverColor: "#F0F6FE",
  borderColor: "#E5E9F0",
  wrapperBorder: false,
  wrapperBorderRadius: 0,
  rowBorder: true,
  columnBorder: false,
  spacing: 9,
  fontSize: 14,
});

export const chartTheme = {
  baseTheme: "ag-default" as const,
  palette: { fills: [brand.blue, brand.navy, brand.sky, brand.gold], strokes: [brand.blue, brand.navy, brand.sky, brand.gold] },
  overrides: { common: { title: { fontSize: 15, fontWeight: 600, color: brand.ink } } },
};

// soft chip colours: tinted background, dark text of the same hue (readable, not shouty)
export const STATUS_CHIP: Record<string, { bg: string; fg: string }> = {
  PROPOSED: { bg: "#FFF1D6", fg: "#7A4B00" }, SENDING: { bg: "#DCEBFC", fg: "#003087" }, SENT: { bg: "#DDF3E6", fg: "#0B5C33" },
  FAILED: { bg: "#FBE1E3", fg: "#8F1521" }, REJECTED: { bg: "#E9ECF1", fg: "#46526B" },
};
