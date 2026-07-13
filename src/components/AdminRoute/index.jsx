import React from 'react'
import { Navigate } from 'react-router-dom'
import useUserStore from '../../store/userStore'

const AdminRoute = ({ children }) => {
  const isLoggedIn = useUserStore((state) => state.isLoggedIn)
  const user = useUserStore((state) => state.user)

  if (!isLoggedIn) {
    return <Navigate to="/login" replace />
  }

  if (user?.role !== 'admin') {
    return <Navigate to="/home" replace />
  }

  return children
}

export default AdminRoute
