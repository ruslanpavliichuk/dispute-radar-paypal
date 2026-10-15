"use client";

import { useEffect, useState } from "react";
import {
  PayPalProvider,
  PayPalOneTimePaymentButton,
} from "@paypal/react-paypal-js/sdk-v6";

type Product = {
  id: string;
  title: string;
  brand: string;
  image_url: string | null;
  price: string;
  currency: string;
};

type Notice = { kind: "ok" | "error"; text: string };

const CLIENT_ID = process.env.NEXT_PUBLIC_PAYPAL_CLIENT_ID;

function formatPrice(p: Product) {
  return new Intl.NumberFormat("en-GB", { style: "currency", currency: p.currency }).format(
    Number(p.price),
  );
}

export default function Shop() {
  const [products, setProducts] = useState<Product[] | null>(null);
  const [notice, setNotice] = useState<Notice | null>(null);

  useEffect(() => {
    fetch("/api/shop/products")
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(String(r.status)))))
      .then(setProducts)
      .catch(() => setNotice({ kind: "error", text: "Could not load products. Is the API running?" }));
  }, []);

  if (!CLIENT_ID) {
    return (
      <p className="p-6 text-red-700">
        NEXT_PUBLIC_PAYPAL_CLIENT_ID is not set. Copy web/.env.local.example to web/.env.local.
      </p>
    );
  }

  async function createOrder(productId: string) {
    const res = await fetch("/api/shop/orders", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ product_id: productId }),
    });
    if (!res.ok) throw new Error(`create order failed: ${res.status}`);
    const { order_id } = await res.json();
    return { orderId: order_id as string };
  }

  async function capture(orderId: string) {
    const res = await fetch(`/api/shop/orders/${encodeURIComponent(orderId)}/capture`, {
      method: "POST",
    });
    if (!res.ok) throw new Error(`capture failed: ${res.status}`);
    const { status, capture_id } = await res.json();
    setNotice({
      kind: "ok",
      text: `Payment ${String(status ?? "received").toLowerCase()}. Order ${orderId}, capture ${capture_id}.`,
    });
  }

  return (
    <PayPalProvider
      clientId={CLIENT_ID}
      environment="sandbox"
      components={["paypal-payments"]}
      pageType="product-listing"
    >
      <main className="mx-auto max-w-5xl p-6">
        <h1 className="text-2xl font-semibold">Dispute Radar demo shop</h1>
        <p className="mt-1 text-sm text-gray-600">
          PayPal sandbox only. Use a sandbox buyer account; no real money moves.
        </p>

        {notice && (
          <p
            role="status"
            className={`mt-4 rounded border p-3 text-sm ${
              notice.kind === "ok"
                ? "border-green-300 bg-green-50 text-green-900"
                : "border-red-300 bg-red-50 text-red-900"
            }`}
          >
            {notice.text}
          </p>
        )}

        {products === null && !notice && <p className="mt-6">Loading products…</p>}

        <ul className="mt-6 grid gap-6 sm:grid-cols-2 lg:grid-cols-3">
          {products?.map((p) => (
            <li key={p.id} className="flex flex-col rounded border p-4">
              {p.image_url && (
                // external Channel3 CDN image; next/image would need a remotePatterns entry
                // eslint-disable-next-line @next/next/no-img-element
                <img
                  src={p.image_url}
                  alt={p.title}
                  referrerPolicy="no-referrer"
                  className="h-40 w-full object-contain"
                />
              )}
              <h2 className="mt-3 font-medium">{p.title}</h2>
              <p className="text-sm text-gray-600">{p.brand}</p>
              <p className="mt-2 text-lg font-semibold">{formatPrice(p)}</p>
              <div className="mt-auto pt-4">
                <PayPalOneTimePaymentButton
                  createOrder={() => createOrder(p.id)}
                  onApprove={async ({ orderId }: { orderId: string }) => capture(orderId)}
                  onCancel={() => setNotice({ kind: "error", text: "Payment cancelled." })}
                  onError={() =>
                    setNotice({ kind: "error", text: "Something went wrong with the payment." })
                  }
                />
              </div>
            </li>
          ))}
        </ul>
      </main>
    </PayPalProvider>
  );
}
