import {
  HomeRoute,
  LoginRoute,
  NotFoundRoute,
  SessionGate,
  SetupRoute,
} from "./routing";
import { createBrowserRouter, type RouteObject } from "react-router-dom";

export const appRoutes: RouteObject[] = [
  {
    element: <SessionGate />,
    children: [
      { path: "/", element: <HomeRoute /> },
      { path: "/setup", element: <SetupRoute /> },
      { path: "/login", element: <LoginRoute /> },
      { path: "/dashboard", element: <HomeRoute /> },
    ],
  },
  { path: "*", element: <NotFoundRoute /> },
];

export function createAppRouter() {
  return createBrowserRouter(appRoutes);
}
