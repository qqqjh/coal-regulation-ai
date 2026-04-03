import React from 'react'
import { Navigate } from 'react-router-dom'
import useUserStore from '../../store/userStore'

const AuthRoute = ({ children }) => {
  const isLoggedIn = useUserStore((state) => state.isLoggedIn)

  if (!isLoggedIn) {
    return <Navigate to="/login" replace />
  }

  return children
}

export default AuthRoute
