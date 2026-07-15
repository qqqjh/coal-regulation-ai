import { Routes, Route } from 'react-router-dom'
import Login from '../pages/Login'
import Home from '../pages/Home'
import Chat from '../pages/Chat'
import Knowledge from '../pages/Knowledge'
import RAG from '../pages/RAG'
import Monitor from '../pages/Monitor'
import Review from '../pages/Review'
import Profile from '../pages/Profile'
import Settings from '../pages/Settings'
import AuthRoute from '../components/AuthRoute'
import AdminRoute from '../components/AdminRoute'
import MainLayout from '../components/MainLayout'
import AdminReviewKb from '../pages/AdminReviewKb'
import Flywheel from '../pages/Flywheel'

const AppRoutes = () => {
  return (
    <Routes>
      {/* 登录页面 - 不需要认证 */}
      <Route path="/login" element={<Login />} />

      {/* 需要认证的页面 */}
      <Route
        path="/*"
        element={
          <AuthRoute>
            <MainLayout />
          </AuthRoute>
        }
      >
        <Route index element={<Home />} />
        <Route path="home" element={<Home />} />
        <Route path="chat" element={<Chat />} />
        <Route path="knowledge" element={<Knowledge />} />
        <Route path="rag" element={<RAG />} />
        <Route path="review" element={<Review />} />
        <Route path="monitor" element={<Monitor />} />
        <Route
          path="admin/review-kb"
          element={
            <AdminRoute>
              <AdminReviewKb />
            </AdminRoute>
          }
        />
        <Route
          path="admin/flywheel"
          element={
            <AdminRoute>
              <Flywheel />
            </AdminRoute>
          }
        />
        <Route path="profile" element={<Profile />} />
        <Route path="settings" element={<Settings />} />
      </Route>
    </Routes>
  )
}

export default AppRoutes
