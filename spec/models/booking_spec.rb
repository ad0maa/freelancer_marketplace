require 'rails_helper'

RSpec.describe Booking, type: :model do
  describe 'validations' do
    it 'is valid with a client, freelancer, dates, and amount' do
      booking = described_class.new(
        client: create(:client),
        freelancer: create(:freelancer),
        start_date: Date.tomorrow,
        end_date: 1.week.from_now.to_date,
        total_amount: 500.00
      )
      expect(booking).to be_valid
    end

    it 'is invalid without a start date' do
      booking = described_class.new(start_date: nil)
      expect(booking).not_to be_valid
      expect(booking.errors[:start_date]).to include("can't be blank")
    end

    it 'is invalid without an end date' do
      booking = described_class.new(end_date: nil)
      expect(booking).not_to be_valid
      expect(booking.errors[:end_date]).to include("can't be blank")
    end

    it 'is invalid when end date is before start date' do
      booking = described_class.new(
        client: create(:client),
        freelancer: create(:freelancer),
        start_date: Date.tomorrow,
        end_date: Date.today,
        total_amount: 500.00
      )
      expect(booking).not_to be_valid
      expect(booking.errors[:end_date]).to include('must be after start date')
    end

    it 'is invalid with a negative total amount' do
      booking = described_class.new(total_amount: -100)
      expect(booking).not_to be_valid
      expect(booking.errors[:total_amount]).to include('must be greater than or equal to 0')
    end
  end

  describe 'status' do
    it 'defaults to pending' do
      booking = described_class.new
      expect(booking.status).to eq('pending')
    end

    it 'can be confirmed' do
      booking = create(:booking)
      booking.confirmed!
      expect(booking).to be_confirmed
    end

    it 'can be completed' do
      booking = create(:booking)
      booking.completed!
      expect(booking).to be_completed
    end

    it 'can be cancelled' do
      booking = create(:booking)
      booking.cancelled!
      expect(booking).to be_cancelled
    end
  end
end
