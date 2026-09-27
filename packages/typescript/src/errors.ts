export class SensesException extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'SensesException'
  }
}
