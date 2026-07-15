import { create } from 'zustand'
import { persist } from 'zustand/middleware'

const useUserStore = create(
  persist(
    (set) => ({
      // 用户信息
      user: null,
      accessToken: null,
      // 是否已登录
      isLoggedIn: false,

      // 登录
      login: (userInfo, accessToken = null) => {
        set({
          user: userInfo,
          accessToken,
          isLoggedIn: true
        })
      },

      // 退出登录
      logout: () => {
        set({
          user: null,
          accessToken: null,
          isLoggedIn: false
        })
      },

      // 更新用户信息
      updateUser: (userInfo) => {
        set((state) => ({
          user: { ...state.user, ...userInfo }
        }))
      }
    }),
    {
      name: 'user-storage', // localStorage 中的 key
    }
  )
)

export default useUserStore
