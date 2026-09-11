import { createAppRouter } from "./router";
import { RouterProvider } from "react-router-dom";

const router = createAppRouter();

export default function App() {
  return <RouterProvider router={router} />;
}
