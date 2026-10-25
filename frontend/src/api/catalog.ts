import type { Product } from "./orders";

export interface Candidate {
  id: string;
  name: string;
  category: string;
  description: string;
  specifications: Record<string, string>;
  suggested_price_cents: number;
  source: "mock_catalog_v1";
  is_demo: true;
  imported_product_id: string | null;
}
export interface CandidatePage {
  items: Candidate[];
  categories: string[];
  has_more: boolean;
}
export interface ImportResult {
  product: Product;
  created: boolean;
}

export function productFields(form: {
  name: string;
  description: string;
  price: string;
  stock: string;
}) {
  const price = form.price.trim(),
    stock = form.stock.trim();
  if (!/^\d{1,7}(\.\d{1,2})?$/.test(price) || !/^\d{1,7}$/.test(stock))
    throw new Error("售价最多保留两位小数，库存须为非负整数");
  const [whole, fraction = ""] = price.split(".");
  const cents = Number(whole) * 100 + Number(fraction.padEnd(2, "0"));
  if (
    cents < 1 ||
    cents > 100000000 ||
    Number(stock) > 1000000 ||
    !form.name.trim() ||
    !form.description.trim()
  )
    throw new Error("请核对名称、说明、售价和库存范围");
  return {
    name: form.name.trim(),
    description: form.description.trim(),
    price_cents: cents,
    stock: Number(stock),
  };
}
