import { createRouter, createWebHashHistory } from "vue-router";

import AdminLayout from "../layouts/AdminLayout.vue";
import CustomerLayout from "../layouts/CustomerLayout.vue";
import HomeView from "../views/HomeView.vue";
import NotFoundView from "../views/NotFoundView.vue";
import StatusView from "../views/StatusView.vue";
import LoginView from "../views/LoginView.vue";
import UsersView from "../views/UsersView.vue";
import AccountView from "../views/AccountView.vue";
import OrdersView from "../views/OrdersView.vue";
import ProductsView from "../views/ProductsView.vue";
import CatalogView from "../views/CatalogView.vue";
import ReturnsView from "../views/ReturnsView.vue";
import { auth } from "../auth/session";

export const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: "/login", component: LoginView, meta: { title: "登录" } },
    {
      path: "/",
      component: CustomerLayout,
      meta: { requiresAuth: true },
      children: [
        { path: "", component: HomeView, meta: { title: "服务首页" } },
        {
          path: "products",
          component: ProductsView,
          meta: { title: "商品目录" },
        },
        { path: "orders", component: OrdersView, meta: { title: "我的订单" } },
        {
          path: "returns",
          component: ReturnsView,
          meta: { title: "我的售后" },
        },
        { path: "status", component: StatusView, meta: { title: "服务状态" } },
        {
          path: "account",
          component: AccountView,
          meta: { title: "账号设置" },
        },
      ],
    },
    {
      path: "/admin",
      component: AdminLayout,
      meta: { requiresAuth: true, adminOnly: true },
      children: [
        { path: "", component: StatusView, meta: { title: "运行概览" } },
        { path: "users", component: UsersView, meta: { title: "账号管理" } },
        {
          path: "returns",
          component: ReturnsView,
          props: { admin: true },
          meta: { title: "售后工单" },
        },
        {
          path: "catalog",
          component: CatalogView,
          meta: { title: "商品管理" },
        },
        {
          path: "orders",
          component: OrdersView,
          props: { admin: true },
          meta: { title: "订单管理" },
        },
      ],
    },
    {
      path: "/:pathMatch(.*)*",
      component: NotFoundView,
      meta: { title: "页面未找到" },
    },
  ],
  scrollBehavior: () => ({ top: 0 }),
});

router.beforeEach(async (to) => {
  if (!to.meta.requiresAuth) return true;
  try {
    const user = await auth.ensureSession();
    if (!user) return { path: "/login", query: { next: to.fullPath } };
    if (to.meta.adminOnly && user.role !== "admin") return "/";
    return true;
  } catch {
    return {
      path: "/login",
      query: { next: to.fullPath, reason: "connection" },
    };
  }
});

router.afterEach((to) => {
  document.title = `${String(to.meta.title || "服务工作台")} · SupportFlow`;
});
