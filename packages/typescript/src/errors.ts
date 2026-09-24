export class SensesError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'SensesError'
  }
}
