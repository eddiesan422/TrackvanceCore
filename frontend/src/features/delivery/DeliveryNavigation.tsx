import { NavLink } from 'react-router-dom'
import { Send, Server } from 'lucide-react'
import { usePermission } from '../../app/session'

export function DeliveryNavigation() {
  const canDelivery = usePermission('delivery:read'), canDestinations = usePermission('destinations:read')
  return <nav className="delivery-navigation" aria-label="Secciones de Data Delivery">{canDelivery && <NavLink to="/delivery" end className={({ isActive }) => isActive ? 'active' : ''}><Send size={16}/> Entregas</NavLink>}{canDestinations && <NavLink to="/delivery/destinations" className={({ isActive }) => isActive ? 'active' : ''}><Server size={16}/> Destinos</NavLink>}</nav>
}
