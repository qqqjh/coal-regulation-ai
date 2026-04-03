import { Routes, Route } from 'react-router-dom'
import Login from '../pages/Login'
import Home from '../pages/Home'
import Chat from '../pages/Chat'
import Knowledge from '../pages/Knowledge'
import RAG from '../pages/RAG'
import Monitor from '../pages/Monitor'
import Review from '../pages/Review'
import AuthRoute from '../components/AuthRoute'
import MainLayout from '../components/MainLayout'

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
      </Route>
    </Routes>
  )
}

export default AppRoutes
