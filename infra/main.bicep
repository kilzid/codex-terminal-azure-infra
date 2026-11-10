targetScope = 'resourceGroup'
param location string
param prefix string = 'codex-terminal'
@allowed(['Standard_B1ms', 'Standard_B2als_v2'])
param vmSize string = 'Standard_B1ms'
@secure()
param adminPublicKey string
param imageVersion string = '0.20261026.2643'
param adminUsername string = 'vmadmin'
param dailyRetentionDays int = 7
param instantRetentionDays int = 2
param memoryPercent int = 15
param cpuCredits int = 10
param auditDays int = 30
@secure()
param ownerObjectId string
param storageEndpointLocations array = [location]
param fileBackupItemNames array = []
@secure()
param workspaceMetadata object = {}
@secure()
param configMetadata object = {}
@secure()
param contactEmail string

var tags = { application: 'codex-terminal', hosting: 'vm' }
resource network 'Microsoft.Network/virtualNetworks@2024-05-01' = {
  name: '${prefix}-vm-vnet'
  location: location
  tags: tags
  properties: { addressSpace: { addressPrefixes: ['10.74.0.0/16'] }, privateEndpointVNetPolicies: 'Disabled' }
}
resource firewall 'Microsoft.Network/networkSecurityGroups@2024-05-01' = {
  name: '${prefix}-vm-nsg'
  location: location
  tags: tags
  properties: { securityRules: [
    { name: 'https', properties: { priority: 100, access: 'Allow', direction: 'Inbound', protocol: 'Tcp', sourceAddressPrefix: '*', sourcePortRange: '*', destinationAddressPrefix: '*', destinationPortRange: '443' } }
    { name: 'http-acme-redirect', properties: { priority: 110, access: 'Allow', direction: 'Inbound', protocol: 'Tcp', sourceAddressPrefix: '*', sourcePortRange: '*', destinationAddressPrefix: '*', destinationPortRange: '80' } }
    { name: 'deny-other-inbound', properties: { priority: 200, access: 'Deny', direction: 'Inbound', protocol: '*', sourceAddressPrefix: '*', sourcePortRange: '*', destinationAddressPrefix: '*', destinationPortRange: '*' } }
  ] }
}
resource subnet 'Microsoft.Network/virtualNetworks/subnets@2024-05-01' = {
  parent: network
  name: 'terminal'
  properties: {
    addressPrefix: '10.74.0.0/24'
    networkSecurityGroup: { id: firewall.id }
    privateEndpointNetworkPolicies: 'Disabled'
    defaultOutboundAccess: false
    serviceEndpoints: retainedRecovery ? [{ service: 'Microsoft.Storage', locations: storageEndpointLocations }] : []
  }
}
resource address 'Microsoft.Network/publicIPAddresses@2024-05-01' = {
  name: '${prefix}-vm-ip'
  location: location
  tags: tags
  sku: { name: 'Standard', tier: 'Regional' }
  properties: {
    publicIPAllocationMethod: 'Static'
    publicIPAddressVersion: 'IPv4'
    ddosSettings: { protectionMode: 'VirtualNetworkInherited' }
    dnsSettings: { domainNameLabel: '${prefix}-${uniqueString(resourceGroup().id)}' }
  }
}
resource nic 'Microsoft.Network/networkInterfaces@2024-05-01' = {
  name: '${prefix}-vm-nic'
  location: location
  tags: tags
  properties: {
    enableIPForwarding: false
    auxiliaryMode: 'None'
    auxiliarySku: 'None'
    disableTcpStateTracking: false
    ipConfigurations: [{ name: 'primary', properties: {
      privateIPAllocationMethod: 'Dynamic'
      privateIPAddressVersion: 'IPv4'
      subnet: { id: subnet.id }
      publicIPAddress: { id: address.id }
    } }]
  }
}
resource vm 'Microsoft.Compute/virtualMachines@2024-11-01' = {
  name: '${prefix}-vm'
  location: location
  tags: tags
  properties: {
    hardwareProfile: { vmSize: vmSize }
    networkProfile: { networkInterfaces: [{ id: nic.id }] }
    osProfile: {
      computerName: 'codex-terminal'
      adminUsername: adminUsername
      customData: base64(loadTextContent('../runtime/cloud-init.yml'))
      linuxConfiguration: {
        provisionVMAgent: true
        disablePasswordAuthentication: true
        ssh: { publicKeys: [{ path: '/home/${adminUsername}/.ssh/authorized_keys', keyData: adminPublicKey }] }
      }
    }
    storageProfile: {
      imageReference: { publisher: 'Debian', offer: 'debian-12', sku: '12-gen2', version: imageVersion }
      osDisk: {
        name: '${prefix}-vm-os'
        createOption: 'FromImage'
        deleteOption: 'Detach'
        caching: 'ReadWrite'
        diskSizeGB: 32
        managedDisk: { storageAccountType: 'StandardSSD_LRS' }
      }
    }
    diagnosticsProfile: { bootDiagnostics: { enabled: true } }
  }
}
module backup './backup.bicep' = {
  name: '${prefix}-vm-backup'
  params: { location: location, prefix: prefix, vmId: vm.id, vmName: vm.name, dailyRetentionDays: dailyRetentionDays, instantRetentionDays: instantRetentionDays, tags: retainedRecovery ? { application: 'codex-terminal', owner: ownerObjectId } : {} }
}
module monitor './monitor.bicep' = {
  name: '${prefix}-vm-monitor'
  params: { prefix: prefix, vmId: vm.id, contactEmail: contactEmail, memoryPercent: memoryPercent, cpuCredits: cpuCredits }
}

// Optional recovery resources stay outside the fresh-install baseline.
param retainedRecovery bool = false
param fileShareBackups bool = false
param auditLogging bool = false
param recoveryStorageName string = ''
param auditWorkspaceName string = ''
module retained './retained-recovery.bicep' = if (retainedRecovery) {
  name: '${prefix}-retained-recovery'
  params: { location: location, storageName: recoveryStorageName, fileShareBackups: fileShareBackups, prefix: prefix, ownerObjectId: ownerObjectId, dailyRetentionDays: dailyRetentionDays, fileBackupItemNames: fileBackupItemNames, workspaceMetadata: workspaceMetadata, configMetadata: configMetadata }
}
module audit './audit.bicep' = if (auditLogging) {
  name: '${prefix}-audit'
  params: { location: location, workspaceName: auditWorkspaceName, auditDays: auditDays, ownerObjectId: ownerObjectId }
}
