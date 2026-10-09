import { BrowserRouter, Route, Routes } from "react-router-dom";
import { Nav } from "./components/Nav";
import { AppContext, useAppState } from "./hooks";
import { AccountPage } from "./pages/Account";
import { ChartPage } from "./pages/Chart";
import { ComparePage } from "./pages/Compare";
import { Dashboard } from "./pages/Dashboard";
import { DataPage } from "./pages/Data";
import { OrdersPage } from "./pages/Orders";
import { SettingsPage } from "./pages/Settings";
import { TodayPage } from "./pages/Today";

export default function App() {
  const state = useAppState();
  return (
    <AppContext.Provider value={state}>
      <BrowserRouter>
        <Nav />
        <main>
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/account" element={<AccountPage />} />
            <Route path="/compare" element={<ComparePage />} />
            <Route path="/today" element={<TodayPage />} />
            <Route path="/orders" element={<OrdersPage />} />
            <Route path="/chart" element={<ChartPage />} />
            <Route path="/data" element={<DataPage />} />
            <Route path="/settings" element={<SettingsPage />} />
            <Route path="*" element={<Dashboard />} />
          </Routes>
        </main>
      </BrowserRouter>
    </AppContext.Provider>
  );
}
